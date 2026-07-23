from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class AllocationCandidate:
    stock_id: int
    symbol: str
    probability: float
    expected_net_return: float
    volatility_20d: float


@dataclass(frozen=True)
class PortfolioAllocation:
    stock_id: int
    symbol: str
    probability: float
    expected_net_return: float
    volatility_20d: float
    score: float
    target_weight: float


@dataclass(frozen=True)
class RejectedAllocation:
    stock_id: int
    symbol: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AllocationResult:
    allocations: list[PortfolioAllocation]
    rejected: list[RejectedAllocation]
    target_total_weight: float
    total_weight: float


@dataclass(frozen=True)
class PlannedBuy:
    quantity: int
    fill_price: float
    notional: float
    commission: float
    transfer_fee: float
    total_cost: float


def _target_total_weight(
    candidates: list[AllocationCandidate],
    *,
    min_total_exposure_pct: float,
    max_total_exposure_pct: float,
) -> float:
    if not candidates:
        return 0.0
    score_sum = sum(
        max(item.probability - 0.50, 0.0)
        * max(item.expected_net_return, 0.0)
        / max(item.volatility_20d, 0.01)
        for item in candidates
    )
    if score_sum <= 0:
        average_probability = sum(item.probability for item in candidates) / len(candidates)
    else:
        average_probability = sum(
            item.probability
            * (
                max(item.probability - 0.50, 0.0)
                * max(item.expected_net_return, 0.0)
                / max(item.volatility_20d, 0.01)
            )
            for item in candidates
        ) / score_sum
    confidence = max(0.0, min(average_probability - 0.55, 0.05)) / 0.05
    scaled = min_total_exposure_pct + confidence * (
        max_total_exposure_pct - min_total_exposure_pct
    )
    return min(max(scaled, min_total_exposure_pct), max_total_exposure_pct)


def _capped_weights(
    scores: list[float],
    *,
    target_total: float,
    cap: float,
) -> list[float]:
    weights = [0.0] * len(scores)
    remaining = set(range(len(scores)))
    remaining_total = target_total
    while remaining and remaining_total > 1e-12:
        score_sum = sum(scores[index] for index in remaining)
        if score_sum <= 0:
            break
        newly_capped: list[int] = []
        for index in sorted(remaining):
            proposed = remaining_total * scores[index] / score_sum
            if proposed >= cap:
                weights[index] = cap
                newly_capped.append(index)
        if not newly_capped:
            for index in remaining:
                weights[index] = remaining_total * scores[index] / score_sum
            break
        for index in newly_capped:
            remaining.remove(index)
            remaining_total -= cap
    return weights


def allocate_portfolio(
    candidates: list[AllocationCandidate],
    *,
    max_positions: int = 10,
    min_probability: float = 0.55,
    min_expected_net_return: float = 0.0,
    min_position_pct: float = 0.02,
    max_position_pct: float = 0.36,
    min_total_exposure_pct: float = 0.30,
    max_total_exposure_pct: float = 0.60,
    volatility_floor: float = 0.01,
) -> AllocationResult:
    eligible: list[tuple[AllocationCandidate, float]] = []
    rejected: list[RejectedAllocation] = []
    for item in candidates:
        reasons: list[str] = []
        if item.probability < min_probability:
            reasons.append("校准盈利概率低于门槛")
        if item.expected_net_return <= min_expected_net_return:
            reasons.append("预期净收益不能覆盖交易成本")
        if item.volatility_20d <= 0:
            reasons.append("20日波动率无效")
        if reasons:
            rejected.append(RejectedAllocation(item.stock_id, item.symbol, tuple(reasons)))
            continue
        score = (
            max(item.probability - 0.50, 0.0)
            * item.expected_net_return
            / max(item.volatility_20d, volatility_floor)
        )
        eligible.append((item, score))

    eligible.sort(key=lambda row: (-row[1], -row[0].probability, row[0].symbol))
    overflow = eligible[max_positions:]
    eligible = eligible[:max_positions]
    rejected.extend(
        RejectedAllocation(item.stock_id, item.symbol, ("超过最大持股数量",))
        for item, _ in overflow
    )
    if not eligible:
        return AllocationResult([], rejected, 0.0, 0.0)

    selected = [item for item, _ in eligible]
    scores = [score for _, score in eligible]
    target_total = _target_total_weight(
        selected,
        min_total_exposure_pct=min_total_exposure_pct,
        max_total_exposure_pct=max_total_exposure_pct,
    )
    active = list(range(len(selected)))
    final_weights: dict[int, float] = {}
    while active:
        active_scores = [scores[index] for index in active]
        remaining_target = max(0.0, target_total - sum(final_weights.values()))
        weights = _capped_weights(
            active_scores,
            target_total=remaining_target,
            cap=max_position_pct,
        )
        too_small = [offset for offset, value in enumerate(weights) if value < min_position_pct]
        if not too_small:
            for offset, index in enumerate(active):
                final_weights[index] = weights[offset]
            break
        removed = {active[offset] for offset in too_small}
        for index in removed:
            rejected.append(
                RejectedAllocation(
                    selected[index].stock_id,
                    selected[index].symbol,
                    ("目标仓位低于最小仓位",),
                )
            )
        active = [index for index in active if index not in removed]

    allocations = [
        PortfolioAllocation(
            stock_id=selected[index].stock_id,
            symbol=selected[index].symbol,
            probability=selected[index].probability,
            expected_net_return=selected[index].expected_net_return,
            volatility_20d=selected[index].volatility_20d,
            score=scores[index],
            target_weight=min(final_weights[index], max_position_pct),
        )
        for index in sorted(final_weights, key=lambda value: (-scores[value], selected[value].symbol))
    ]
    total_weight = min(sum(item.target_weight for item in allocations), max_total_exposure_pct)
    return AllocationResult(allocations, rejected, target_total, total_weight)


def plan_buy_quantity(
    *,
    total_asset: float,
    available_cash: float,
    target_weight: float,
    market_price: float,
    slippage_bps: float,
    commission_rate: float,
    min_commission: float,
    transfer_fee_rate: float,
) -> PlannedBuy:
    if total_asset <= 0 or available_cash <= 0 or target_weight <= 0 or market_price <= 0:
        return PlannedBuy(0, 0.0, 0.0, 0.0, 0.0, 0.0)
    fill_price = market_price * (1 + slippage_bps / 10_000)
    budget = min(total_asset * target_weight, available_cash)
    quantity = math.floor(budget / fill_price / 100) * 100
    while quantity > 0:
        notional = fill_price * quantity
        commission = max(notional * commission_rate, min_commission)
        transfer_fee = notional * transfer_fee_rate
        total_cost = notional + commission + transfer_fee
        if total_cost <= budget and total_cost <= available_cash:
            return PlannedBuy(
                quantity,
                fill_price,
                notional,
                commission,
                transfer_fee,
                total_cost,
            )
        quantity -= 100
    return PlannedBuy(0, fill_price, 0.0, 0.0, 0.0, 0.0)
