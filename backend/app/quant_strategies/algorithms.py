from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import math
import statistics
from typing import Any, Iterable

from .catalog import QUANT_STRATEGY_SPECS


@dataclass(frozen=True)
class PriceBar:
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    adjusted_close: float


@dataclass(frozen=True)
class FinancialPoint:
    report_period: date
    actual_announcement_date: date
    available_on: date
    eps: float | None = None
    roe: float | None = None
    gross_margin: float | None = None
    operating_cash_flow: float | None = None
    net_profit: float | None = None
    revenue: float | None = None
    total_assets: float | None = None
    total_liabilities: float | None = None


@dataclass(frozen=True)
class CandidateInput:
    symbol: str
    name: str
    instrument_type: str
    bars: tuple[PriceBar, ...]
    financial: FinancialPoint | None
    metric: dict[str, float]
    financial_history: tuple[FinancialPoint, ...] = ()


@dataclass(frozen=True)
class TargetPortfolio:
    strategy_key: str
    target_weights: dict[str, float]
    scores: dict[str, float]
    features: dict[str, dict[str, float]]
    rejected: dict[str, tuple[str, ...]]
    exit_after_trading_days: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def financial_point_available(point: FinancialPoint, as_of: date) -> bool:
    return point.available_on <= as_of and point.actual_announcement_date <= as_of


def _prices(item: CandidateInput) -> list[float]:
    return [float(bar.adjusted_close) for bar in item.bars if bar.adjusted_close > 0]


def _returns(values: list[float]) -> list[float]:
    return [values[index] / values[index - 1] - 1 for index in range(1, len(values))]


def _return_between(values: list[float], start_offset: int, end_offset: int = 0) -> float:
    end_index = len(values) - 1 - end_offset
    start_index = len(values) - 1 - start_offset
    if start_index < 0 or end_index <= start_index:
        raise ValueError("日线长度不足")
    return values[end_index] / values[start_index] - 1


def _volatility(values: list[float], days: int) -> float:
    returns = _returns(values[-(days + 1) :])
    if len(returns) < 2:
        return 0.0
    return statistics.pstdev(returns) * math.sqrt(252)


def _mean(values: Iterable[float]) -> float:
    rows = list(values)
    return sum(rows) / len(rows) if rows else 0.0


def _percentiles(values: dict[str, float], *, descending: bool = False) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda row: ((-row[1]) if descending else row[1], row[0]))
    if len(ordered) == 1:
        return {ordered[0][0]: 1.0}
    return {symbol: rank / (len(ordered) - 1) for rank, (symbol, _) in enumerate(ordered)}


def _capped_weights(
    scores: dict[str, float],
    *,
    total: float,
    cap: float,
    minimum: float = 0.0,
) -> dict[str, float]:
    active = {symbol: max(float(score), 0.0) for symbol, score in scores.items()}
    if not active or total <= 0:
        return {}
    if not any(active.values()):
        active = {symbol: 1.0 for symbol in active}
    result = {symbol: 0.0 for symbol in active}
    remaining = min(total, cap * len(active))
    open_symbols = set(active)
    while open_symbols and remaining > 1e-12:
        denominator = sum(active[symbol] for symbol in open_symbols)
        if denominator <= 0:
            proposals = {symbol: remaining / len(open_symbols) for symbol in open_symbols}
        else:
            proposals = {
                symbol: remaining * active[symbol] / denominator
                for symbol in open_symbols
            }
        capped = {symbol for symbol, value in proposals.items() if value >= cap - result[symbol]}
        if not capped:
            for symbol, value in proposals.items():
                result[symbol] += value
            remaining = 0.0
            break
        for symbol in sorted(capped):
            addition = cap - result[symbol]
            result[symbol] = cap
            remaining -= addition
            open_symbols.remove(symbol)
    filtered = {symbol: weight for symbol, weight in result.items() if weight + 1e-12 >= minimum}
    return dict(sorted(filtered.items()))


def _common_features(item: CandidateInput) -> tuple[dict[str, float], tuple[str, ...]]:
    values = _prices(item)
    if len(values) < 253:
        return {}, ("已完成复权日线不足253根",)
    features = {
        "momentum_12_1": _return_between(values, 252, 21),
        "momentum_6_1": _return_between(values, 126, 21),
        "volatility_60d": _volatility(values, 60),
        "ma60": _mean(values[-60:]),
        "ma200": _mean(values[-200:]),
        "ma200_slope_20d": _mean(values[-200:]) - _mean(values[-220:-20]),
        "last_close": values[-1],
    }
    return features, ()


def _financial_features(
    item: CandidateInput,
    as_of: date,
) -> tuple[dict[str, float], tuple[str, ...]]:
    point = item.financial
    if point is None:
        return {}, ("缺少点时财务数据",)
    if not financial_point_available(point, as_of):
        return {}, ("财务数据在决策时尚不可见",)
    required = (
        point.roe,
        point.gross_margin,
        point.operating_cash_flow,
        point.total_assets,
        point.total_liabilities,
    )
    if any(value is None for value in required) or not point.total_assets:
        return {}, ("点时财务字段不完整",)
    return {
        "roe": float(point.roe),
        "gross_margin": float(point.gross_margin),
        "cash_flow_to_assets": float(point.operating_cash_flow) / float(point.total_assets),
        "debt_ratio": float(point.total_liabilities) / float(point.total_assets),
    }, ()


def _multi_factor(
    items: list[CandidateInput],
    as_of: date,
    *,
    parameters: dict[str, Any],
    low_vol_quality: bool = False,
) -> TargetPortfolio:
    rejected: dict[str, tuple[str, ...]] = {}
    raw: dict[str, dict[str, float]] = {}
    for item in items:
        if low_vol_quality:
            values = _prices(item)
            if len(values) < 61:
                common, common_reasons = {}, ("已完成复权日线不足61根",)
            else:
                common, common_reasons = {
                    "volatility_60d": _volatility(values, 60),
                    "last_close": values[-1],
                }, ()
        else:
            common, common_reasons = _common_features(item)
        financial, financial_reasons = _financial_features(item, as_of)
        reasons = (*common_reasons, *financial_reasons)
        pe = float(item.metric.get("pe_ttm", 0) or 0)
        pb = float(item.metric.get("pb", 0) or 0)
        if not low_vol_quality and (pe <= 0 or pb <= 0):
            reasons = (*reasons, "估值指标无效")
        if financial and (financial["roe"] <= 0 or financial["cash_flow_to_assets"] <= 0):
            reasons = (*reasons, "质量底线未通过")
        if reasons:
            rejected[item.symbol] = tuple(dict.fromkeys(reasons))
            continue
        raw[item.symbol] = {
            **common,
            **financial,
            "earnings_yield": 1 / pe if pe > 0 else 0,
            "book_to_price": 1 / pb if pb > 0 else 0,
        }
    if not raw:
        key = "low_vol_quality" if low_vol_quality else "multi_factor_core"
        return TargetPortfolio(key, {}, {}, {}, rejected)

    factor_names = (
        (
            "roe",
            "gross_margin",
            "cash_flow_to_assets",
            "debt_ratio",
            "volatility_60d",
        )
        if low_vol_quality
        else (
            "earnings_yield",
            "book_to_price",
            "roe",
            "gross_margin",
            "cash_flow_to_assets",
            "debt_ratio",
            "momentum_12_1",
            "momentum_6_1",
            "volatility_60d",
        )
    )
    percentiles = {
        name: _percentiles(
            {symbol: values[name] for symbol, values in raw.items()},
            descending=name in {"volatility_60d", "debt_ratio"},
        )
        for name in factor_names
    }
    scores: dict[str, float] = {}
    for symbol in raw:
        quality = (
            percentiles["roe"][symbol] * 0.30
            + percentiles["gross_margin"][symbol] * 0.20
            + percentiles["cash_flow_to_assets"][symbol] * 0.30
            + percentiles["debt_ratio"][symbol] * 0.20
        )
        low_vol = percentiles["volatility_60d"][symbol]
        if low_vol_quality:
            scores[symbol] = (
                quality * float(parameters["quality_weight"])
                + low_vol * float(parameters["low_vol_weight"])
            )
        else:
            value = (
                percentiles["earnings_yield"][symbol]
                + percentiles["book_to_price"][symbol]
            ) / 2
            momentum = (
                percentiles["momentum_12_1"][symbol] * 0.60
                + percentiles["momentum_6_1"][symbol] * 0.40
            )
            scores[symbol] = (
                value * float(parameters["value_weight"])
                + quality * float(parameters["quality_weight"])
                + momentum * float(parameters["momentum_weight"])
                + low_vol * float(parameters["low_vol_weight"])
            )
    selected = dict(sorted(scores.items(), key=lambda row: (-row[1], row[0]))[:10])
    spec = QUANT_STRATEGY_SPECS[
        "low_vol_quality" if low_vol_quality else "multi_factor_core"
    ]
    weights = _capped_weights(
        selected,
        total=spec.max_total_exposure_pct,
        cap=spec.max_position_pct,
        minimum=float(parameters.get("min_position_pct", 0)),
    )
    key = "low_vol_quality" if low_vol_quality else "multi_factor_core"
    return TargetPortfolio(key, weights, scores, raw, rejected)


def _relative_strength(
    items: list[CandidateInput],
    as_of: date,
    *,
    parameters: dict[str, Any],
) -> TargetPortfolio:
    raw: dict[str, dict[str, float]] = {}
    rejected: dict[str, tuple[str, ...]] = {}
    for item in items:
        features, reasons = _common_features(item)
        if not reasons and (
            features["last_close"] <= features["ma200"]
            or features["ma200_slope_20d"] <= 0
        ):
            reasons = ("MA200趋势过滤未通过",)
        if reasons:
            rejected[item.symbol] = reasons
        else:
            raw[item.symbol] = features
    if not raw:
        return TargetPortfolio("relative_strength_rotation", {}, {}, {}, rejected)
    p12 = _percentiles({symbol: row["momentum_12_1"] for symbol, row in raw.items()})
    p6 = _percentiles({symbol: row["momentum_6_1"] for symbol, row in raw.items()})
    scores = {
        symbol: (
            p12[symbol] * float(parameters["momentum_12_1_weight"])
            + p6[symbol] * float(parameters["momentum_6_1_weight"])
        )
        for symbol in raw
    }
    selected = dict(sorted(scores.items(), key=lambda row: (-row[1], row[0]))[:5])
    return TargetPortfolio(
        "relative_strength_rotation",
        _capped_weights(selected, total=0.70, cap=0.20),
        scores,
        raw,
        rejected,
    )


def _atr(bars: tuple[PriceBar, ...], days: int = 20) -> float:
    ranges = []
    for previous, current in zip(bars[-(days + 1) : -1], bars[-days:]):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return _mean(ranges)


def _breakout(
    items: list[CandidateInput],
    as_of: date,
    *,
    parameters: dict[str, Any],
) -> TargetPortfolio:
    scores: dict[str, float] = {}
    features_by_symbol: dict[str, dict[str, float]] = {}
    rejected: dict[str, tuple[str, ...]] = {}
    for item in items:
        common, reasons = _common_features(item)
        if reasons:
            rejected[item.symbol] = reasons
            continue
        breakout_days = int(parameters["breakout_days"])
        previous = item.bars[-(breakout_days + 1) : -1]
        if len(previous) < breakout_days:
            rejected[item.symbol] = (f"前{breakout_days}日突破历史不足",)
            continue
        prior_high = max(bar.high for bar in previous)
        average_volume = _mean(bar.volume for bar in item.bars[-21:-1])
        current = item.bars[-1]
        atr = _atr(item.bars)
        local_reasons = []
        if current.close <= prior_high:
            local_reasons.append("未突破前55日高点")
        if common["ma200_slope_20d"] <= 0:
            local_reasons.append("MA200趋势未向上")
        if (
            average_volume <= 0
            or current.volume
            < average_volume * float(parameters["volume_confirmation"])
        ):
            local_reasons.append("成交量确认不足")
        if atr <= 0:
            local_reasons.append("ATR无效")
        if local_reasons:
            rejected[item.symbol] = tuple(local_reasons)
            continue
        score = ((current.close / prior_high) - 1) / (atr / current.close)
        score *= current.volume / average_volume
        scores[item.symbol] = score
        features_by_symbol[item.symbol] = {
            **common,
            f"prior_{breakout_days}d_high": prior_high,
            "average_volume_20d": average_volume,
            "atr_20d": atr,
        }
    selected = dict(sorted(scores.items(), key=lambda row: (-row[1], row[0]))[:8])
    risk_scores = {
        symbol: 1 / max(features_by_symbol[symbol]["atr_20d"], 1e-9)
        for symbol in selected
    }
    return TargetPortfolio(
        "breakout_trend",
        _capped_weights(risk_scores, total=0.60, cap=0.15),
        scores,
        features_by_symbol,
        rejected,
        metadata={
            "exit_days": int(parameters["exit_days"]),
            "atr_multiple": float(parameters["atr_multiple"]),
        },
    )


def _volume_price_confirmation(
    items: list[CandidateInput],
    as_of: date,
    *,
    parameters: dict[str, Any],
) -> TargetPortfolio:
    spec = QUANT_STRATEGY_SPECS["volume_price_confirmation"]
    scores: dict[str, float] = {}
    features_by_symbol: dict[str, dict[str, Any]] = {}
    risk_weights: dict[str, float] = {}
    rejected: dict[str, tuple[str, ...]] = {}

    anchor_lookback = int(parameters["anchor_lookback_days"])
    fast_days = int(parameters["ma_fast_days"])
    slow_days = int(parameters["ma_slow_days"])
    slope_days = int(parameters["ma_slope_days"])
    high_lookback = int(parameters["high_position_lookback"])
    required_bars = max(
        slow_days + slope_days,
        high_lookback + anchor_lookback + 1,
        21 + anchor_lookback,
    )

    for item in items:
        bars = tuple(bar for bar in item.bars if bar.trade_date <= as_of)
        if len(bars) < required_bars:
            rejected[item.symbol] = (
                f"已完成日线不足{required_bars}根",
            )
            continue

        current = bars[-1]
        previous_twenty = bars[-21:-1]
        average_turnover = _mean(bar.amount for bar in previous_twenty)
        if average_turnover < float(parameters["min_average_turnover"]):
            rejected[item.symbol] = ("20日平均成交额不足1亿元",)
            continue

        anchor_row: tuple[int, PriceBar, float, float] | None = None
        for age in range(1, anchor_lookback + 1):
            anchor_index = len(bars) - 1 - age
            anchor = bars[anchor_index]
            prior_twenty = bars[anchor_index - 20 : anchor_index]
            prior_three = bars[anchor_index - 3 : anchor_index]
            if len(prior_twenty) < 20 or len(prior_three) < 3:
                continue
            average_volume = _mean(bar.volume for bar in prior_twenty)
            if (
                average_volume <= 0
                or anchor.volume
                < average_volume * float(parameters["anchor_volume_multiple"])
                or anchor.volume <= max(bar.volume for bar in prior_three)
            ):
                continue
            anchor_row = (anchor_index, anchor, average_volume, anchor.volume / average_volume)
            break

        if anchor_row is None:
            rejected[item.symbol] = ("最近3日无有效放量锚点",)
            continue

        anchor_index, anchor, anchor_average_volume, anchor_volume_ratio = anchor_row
        support = min(anchor.open, anchor.close)
        resistance = max(anchor.open, anchor.close)
        reasons: list[str] = []

        if any(bar.close < support for bar in bars[anchor_index + 1 :]):
            reasons.append("锚点支撑失守")

        latest_three = bars[-3:]
        if (
            len(latest_three) == 3
            and latest_three[0].high > latest_three[1].high > latest_three[2].high
            and latest_three[0].low > latest_three[1].low > latest_three[2].low
        ):
            reasons.append("最近3日高低点连续下移")

        anchor_range = anchor.high - anchor.low
        anchor_body = abs(anchor.close - anchor.open)
        prior_highs = bars[max(0, anchor_index - high_lookback) : anchor_index]
        if (
            anchor_range > 0
            and prior_highs
            and anchor.high >= max(bar.high for bar in prior_highs)
            and anchor_body / anchor_range
            <= float(parameters["small_body_ratio"])
        ):
            reasons.append("60日高位放量小实体")

        current_body = abs(current.close - current.open)
        current_upper_shadow = current.high - max(current.open, current.close)
        if (
            current.high >= resistance
            and current_upper_shadow
            >= float(parameters["upper_shadow_body_multiple"])
            * max(current_body, current.close * 1e-6)
        ):
            reasons.append("压力位附近长上影")

        adjusted = [
            float(bar.adjusted_close)
            for bar in bars
            if bar.adjusted_close > 0
        ]
        if len(adjusted) < required_bars:
            rejected[item.symbol] = (
                f"已完成复权日线不足{required_bars}根",
            )
            continue
        adjusted_current = adjusted[-1]
        ma20 = _mean(adjusted[-fast_days:])
        ma60 = _mean(adjusted[-slow_days:])
        ma20_before = _mean(
            adjusted[-(fast_days + slope_days) : -slope_days]
        )
        if not (
            adjusted_current > ma20 > ma60
            and ma20 > ma20_before
        ):
            reasons.append("MA20与MA60趋势过滤未通过")
        if current.close <= resistance:
            reasons.append("确认日未突破锚点压力")

        confirmation_average_volume = _mean(
            bar.volume for bar in previous_twenty
        )
        confirmation_volume_ratio = (
            current.volume / confirmation_average_volume
            if confirmation_average_volume > 0
            else 0
        )
        if confirmation_volume_ratio < float(
            parameters["confirmation_volume_multiple"]
        ):
            reasons.append("确认日成交量不足")

        current_range = current.high - current.low
        close_location = (
            (current.close - current.low) / current_range
            if current_range > 0
            else 0
        )
        if close_location < float(parameters["close_location_min"]):
            reasons.append("确认日收盘位置不足")

        atr = _atr(bars)
        if atr <= 0:
            reasons.append("ATR无效")
        if reasons:
            rejected[item.symbol] = tuple(dict.fromkeys(reasons))
            continue

        interim = bars[anchor_index + 1 : -1]
        pullback_quality = (
            0.5
            if not interim
            else float(
                _mean(bar.volume for bar in interim)
                <= anchor.volume * float(parameters["pullback_volume_ratio"])
            )
        )
        score = (
            min(
                anchor_volume_ratio
                / float(parameters["anchor_volume_multiple"]),
                1.5,
            )
            + min(max((current.close - resistance) / atr, 0), 1.5)
            + min(max((ma20 / ma60 - 1) / 0.01, 0), 1.0)
            + min(max((ma20 / ma20_before - 1) / 0.005, 0), 1.0)
            + min(max(close_location, 0), 1.0)
            + pullback_quality
        )
        if score < float(parameters["score_threshold"]):
            rejected[item.symbol] = ("综合置信分不足",)
            continue

        effective_stop = max(
            support,
            current.close * (1 - float(parameters["hard_stop_pct"])),
        )
        stop_distance_pct = (current.close - effective_stop) / current.close
        if stop_distance_pct <= 0:
            rejected[item.symbol] = ("止损距离无效",)
            continue
        target_weight = min(
            spec.max_position_pct,
            float(parameters["risk_per_trade_pct"]) / stop_distance_pct,
        )
        if target_weight < float(parameters["min_position_pct"]):
            rejected[item.symbol] = ("风险反推仓位不足5%",)
            continue

        scores[item.symbol] = score
        risk_weights[item.symbol] = target_weight
        features_by_symbol[item.symbol] = {
            "anchor_date": anchor.trade_date.isoformat(),
            "anchor_age_days": len(bars) - 1 - anchor_index,
            "anchor_support": support,
            "anchor_resistance": resistance,
            "anchor_average_volume_20d": anchor_average_volume,
            "anchor_volume_ratio": anchor_volume_ratio,
            "confirmation_average_volume_20d": confirmation_average_volume,
            "confirmation_volume_ratio": confirmation_volume_ratio,
            "average_turnover_20d": average_turnover,
            "ma20": ma20,
            "ma60": ma60,
            "ma20_slope_reference": ma20_before,
            "atr_20d": atr,
            "close_location": close_location,
            "pullback_quality": pullback_quality,
            "confidence_score": score,
            "signal_version": "volume-price-confirmation-v1",
            "entry_reference_price": current.close,
            "effective_stop": effective_stop,
            "stop_distance_pct": stop_distance_pct,
            "risk_weight": target_weight,
        }

    selected_symbols = [
        symbol
        for symbol, _ in sorted(
            scores.items(),
            key=lambda row: (-row[1], row[0]),
        )[: spec.max_positions]
    ]
    selected_weights = {
        symbol: risk_weights[symbol]
        for symbol in selected_symbols
    }
    total_weight = sum(selected_weights.values())
    if total_weight > spec.max_total_exposure_pct:
        scale = spec.max_total_exposure_pct / total_weight
        selected_weights = {
            symbol: weight * scale
            for symbol, weight in selected_weights.items()
            if weight * scale + 1e-12
            >= float(parameters["min_position_pct"])
        }

    return TargetPortfolio(
        "volume_price_confirmation",
        dict(sorted(selected_weights.items())),
        scores,
        features_by_symbol,
        rejected,
        metadata={
            "signal_version": "volume-price-confirmation-v1",
            "hard_stop_pct": float(parameters["hard_stop_pct"]),
            "atr_trailing_multiple": float(
                parameters["atr_trailing_multiple"]
            ),
            "max_holding_days": int(parameters["max_holding_days"]),
            "min_holding_gain_pct": float(
                parameters["min_holding_gain_pct"]
            ),
            "execution_risk_recheck": True,
        },
    )


def _short_term_reversal(
    items: list[CandidateInput],
    benchmark: CandidateInput | None,
    as_of: date,
    *,
    parameters: dict[str, Any],
) -> TargetPortfolio:
    if benchmark is None or len(_prices(benchmark)) < 61:
        return TargetPortfolio(
            "short_term_reversal_t1", {}, {}, {},
            {item.symbol: ("基准日线不足",) for item in items},
            exit_after_trading_days=int(parameters["holding_days"]),
        )
    benchmark_values = _prices(benchmark)
    benchmark_one = _return_between(benchmark_values, 1)
    benchmark_five = _return_between(benchmark_values, 5)
    scores: dict[str, float] = {}
    features_by_symbol: dict[str, dict[str, float]] = {}
    rejected: dict[str, tuple[str, ...]] = {}
    for item in items:
        values = _prices(item)
        if len(values) < 61:
            rejected[item.symbol] = ("已完成复权日线不足61根",)
            continue
        one = _return_between(values, 1) - benchmark_one
        five = _return_between(values, 5) - benchmark_five
        ma60 = _mean(values[-60:])
        reasons = []
        if values[-1] <= ma60:
            reasons.append("MA60趋势过滤未通过")
        one_day_threshold = float(parameters["one_day_residual"])
        five_day_threshold = float(parameters["five_day_residual"])
        if one >= one_day_threshold:
            reasons.append("1日残差未达到超跌门槛")
        if five >= five_day_threshold:
            reasons.append("5日残差未达到超跌门槛")
        if reasons:
            rejected[item.symbol] = tuple(reasons)
            continue
        volatility = max(_volatility(values, 20), 0.01)
        score = (
            (one_day_threshold - one) + (five_day_threshold - five)
        ) / volatility
        scores[item.symbol] = score
        features_by_symbol[item.symbol] = {
            "one_day_residual": one,
            "five_day_residual": five,
            "ma60": ma60,
            "volatility_20d": volatility,
        }
    selected = dict(sorted(scores.items(), key=lambda row: (-row[1], row[0]))[:5])
    return TargetPortfolio(
        "short_term_reversal_t1",
        _capped_weights(selected, total=0.40, cap=0.10),
        scores,
        features_by_symbol,
        rejected,
        exit_after_trading_days=int(parameters["holding_days"]),
    )


def _single_quarter_eps(points: list[FinancialPoint]) -> dict[tuple[int, int], float]:
    cumulative = {
        (point.report_period.year, (point.report_period.month - 1) // 3 + 1): float(point.eps)
        for point in points
        if point.eps is not None
    }
    quarterly: dict[tuple[int, int], float] = {}
    for (year, quarter), value in sorted(cumulative.items()):
        prior = cumulative.get((year, quarter - 1)) if quarter > 1 else 0.0
        if prior is not None:
            quarterly[(year, quarter)] = value - prior
    return quarterly


def _earnings_sue(item: CandidateInput) -> float | None:
    point = item.financial
    if point is None or point.eps is None:
        return None
    points = sorted(
        [
            row
            for row in (*item.financial_history, point)
            if row.eps is not None
        ],
        key=lambda row: row.report_period,
    )
    quarterly = _single_quarter_eps(points)
    current_key = (
        point.report_period.year,
        (point.report_period.month - 1) // 3 + 1,
    )
    prior_year_key = (current_key[0] - 1, current_key[1])
    if current_key not in quarterly or prior_year_key not in quarterly:
        return None
    historical_surprises = []
    for key, value in sorted(quarterly.items()):
        if key >= current_key:
            continue
        prior_value = quarterly.get((key[0] - 1, key[1]))
        if prior_value is not None:
            historical_surprises.append(value - prior_value)
    if len(historical_surprises) < 8:
        return None
    current_surprise = quarterly[current_key] - quarterly[prior_year_key]
    baseline = historical_surprises[-8:]
    scale = statistics.pstdev(baseline) if len(baseline) > 1 else 0.0
    scale = max(scale, abs(_mean(baseline)) * 0.25, 0.01)
    return current_surprise / scale


def _earnings_drift(
    items: list[CandidateInput],
    as_of: date,
    *,
    parameters: dict[str, Any],
) -> TargetPortfolio:
    scores: dict[str, float] = {}
    features_by_symbol: dict[str, dict[str, float]] = {}
    rejected: dict[str, tuple[str, ...]] = {}
    for item in items:
        point = item.financial
        if point is None:
            rejected[item.symbol] = ("缺少点时财务数据",)
            continue
        if not financial_point_available(point, as_of):
            rejected[item.symbol] = ("财务数据在决策时尚不可见",)
            continue
        if point.available_on != as_of:
            rejected[item.symbol] = ("不在公告后首个可交易日",)
            continue
        sue = _earnings_sue(item)
        if sue is None:
            rejected[item.symbol] = ("业绩意外历史不足",)
            continue
        values = _prices(item)
        if len(values) < 2:
            rejected[item.symbol] = ("价格确认日线不足",)
            continue
        confirmation = _return_between(values, 1)
        reasons = []
        if sue < float(parameters["min_sue"]):
            reasons.append("标准化业绩意外值不足1")
        if confirmation <= 0:
            reasons.append("公告后价格确认未通过")
        if reasons:
            rejected[item.symbol] = tuple(reasons)
            continue
        scores[item.symbol] = sue * (1 + confirmation)
        features_by_symbol[item.symbol] = {
            "sue": sue,
            "price_confirmation": confirmation,
            "report_period": point.report_period.isoformat(),
        }
    selected = dict(sorted(scores.items(), key=lambda row: (-row[1], row[0]))[:10])
    return TargetPortfolio(
        "earnings_drift",
        _capped_weights(selected, total=0.70, cap=0.10),
        scores,
        features_by_symbol,
        rejected,
        exit_after_trading_days=int(parameters["holding_days"]),
    )


def _regime_allocator(
    items: list[CandidateInput],
    benchmark: CandidateInput | None,
    as_of: date,
    *,
    parameters: dict[str, Any],
) -> TargetPortfolio:
    expected = {item.symbol: item for item in items if item.instrument_type == "ETF"}
    configured = list(parameters["etf_universe"])
    missing = sorted(set(configured) - set(expected))
    if missing:
        return TargetPortfolio(
            "regime_allocator",
            {},
            {},
            {},
            {symbol: ("配置ETF缺失",) for symbol in missing},
        )
    invalid = {
        item.symbol: ("ETF日线不足200根",)
        for item in items
        if len(_prices(item)) < 200
    }
    if benchmark is None or len(_prices(benchmark)) < 220 or invalid:
        rejected = dict(invalid)
        if benchmark is None or len(_prices(benchmark)) < 220:
            rejected[benchmark.symbol if benchmark else "benchmark"] = ("基准日线不足220根",)
        return TargetPortfolio("regime_allocator", {}, {}, {}, rejected)
    values = _prices(benchmark)
    ma50 = _mean(values[-50:])
    ma200 = _mean(values[-200:])
    slope = ma50 - _mean(values[-70:-20])
    volatility = _volatility(values, 20)
    large_cap, mid_cap, growth, dividend, bond, gold = configured[:6]
    if values[-1] > ma200 and ma50 > ma200 and slope > 0 and volatility <= 0.25:
        regime = "risk_on"
        desired = {
            large_cap: 0.30,
            mid_cap: 0.20,
            growth: 0.20,
            dividend: 0.10,
        }
    elif values[-1] < ma200 and slope < 0:
        regime = "risk_off"
        desired = {bond: 0.50, gold: 0.20}
    else:
        regime = "neutral"
        desired = {
            large_cap: 0.25,
            mid_cap: 0.15,
            growth: 0.10,
            bond: 0.20,
        }
    weights = {symbol: weight for symbol, weight in desired.items() if symbol in expected}
    return TargetPortfolio(
        "regime_allocator",
        weights,
        {symbol: weight for symbol, weight in weights.items()},
        {"benchmark": {"ma50": ma50, "ma200": ma200, "slope": slope, "volatility_20d": volatility}},
        {},
        metadata={"regime": regime},
    )


def _covariance_matrix(series: list[list[float]]) -> list[list[float]]:
    columns = len(series)
    means = [_mean(row) for row in series]
    observations = min(len(row) for row in series)
    raw = []
    for left in range(columns):
        row = []
        for right in range(columns):
            covariance = _mean(
                (series[left][index] - means[left]) * (series[right][index] - means[right])
                for index in range(observations)
            )
            row.append(covariance)
        raw.append(row)
    average_variance = _mean(raw[index][index] for index in range(columns))
    floor = max(average_variance * 1e-3, 1e-10)
    shrinkage = 0.10
    return [
        [
            raw[left][right] * (1 - shrinkage)
            + (max(raw[left][left], floor) if left == right else 0) * shrinkage
            + (floor if left == right else 0)
            for right in range(columns)
        ]
        for left in range(columns)
    ]


def _portfolio_volatility(weights: list[float], covariance: list[list[float]]) -> float:
    variance = sum(
        weights[left] * weights[right] * covariance[left][right]
        for left in range(len(weights))
        for right in range(len(weights))
    )
    return math.sqrt(max(variance, 0.0)) * math.sqrt(252)


def _risk_parity(
    items: list[CandidateInput],
    as_of: date,
    *,
    parameters: dict[str, Any],
) -> TargetPortfolio:
    configured = set(parameters["etf_universe"])
    present = {item.symbol for item in items if item.instrument_type == "ETF"}
    missing = sorted(configured - present)
    if missing:
        return TargetPortfolio(
            "risk_parity_overlay",
            {},
            {},
            {},
            {symbol: ("配置ETF缺失",) for symbol in missing},
            metadata={"leveraged": False, "converged": False},
        )
    valid: list[CandidateInput] = []
    rejected: dict[str, tuple[str, ...]] = {}
    for item in sorted(items, key=lambda row: row.symbol):
        values = _prices(item)
        lookback_days = int(parameters["lookback_days"])
        if item.instrument_type != "ETF" or len(values) < lookback_days + 1:
            rejected[item.symbol] = (f"ETF日线不足{lookback_days}根",)
        else:
            valid.append(item)
    if rejected:
        return TargetPortfolio(
            "risk_parity_overlay",
            {},
            {},
            {},
            rejected,
            metadata={"leveraged": False, "converged": False},
        )
    if not valid:
        return TargetPortfolio("risk_parity_overlay", {}, {}, {}, rejected, metadata={"leveraged": False})
    returns = [
        _returns(_prices(item)[-(int(parameters["lookback_days"]) + 1) :])
        for item in valid
    ]
    covariance = _covariance_matrix(returns)
    count = len(valid)
    weights = [1 / count] * count
    converged = False
    for _ in range(1000):
        marginal = [
            sum(covariance[index][other] * weights[other] for other in range(count))
            for index in range(count)
        ]
        contributions = [max(weights[index] * marginal[index], 1e-14) for index in range(count)]
        target = sum(contributions) / count
        updated = [weights[index] * math.sqrt(target / contributions[index]) for index in range(count)]
        total = sum(updated)
        updated = [value / total for value in updated]
        if max(abs(updated[index] - weights[index]) for index in range(count)) < 1e-9:
            weights = updated
            converged = True
            break
        weights = updated
    if not converged:
        return TargetPortfolio(
            "risk_parity_overlay",
            {},
            {},
            {},
            {item.symbol: ("风险平价权重未收敛",) for item in valid},
            metadata={"leveraged": False, "converged": False},
        )
    annualized_volatility = _portfolio_volatility(weights, covariance)
    target_volatility = float(parameters["target_volatility"])
    scale = (
        min(0.80, target_volatility / annualized_volatility)
        if annualized_volatility > 0
        else 0.80
    )
    raw_scores = {item.symbol: weights[index] for index, item in enumerate(valid)}
    allocated = _capped_weights(raw_scores, total=scale, cap=0.35)
    allocated = {
        symbol: weight
        for symbol, weight in allocated.items()
        if weight >= float(parameters["min_weight"])
    }
    return TargetPortfolio(
        "risk_parity_overlay",
        allocated,
        raw_scores,
        {},
        rejected,
        metadata={
            "leveraged": False,
            "converged": converged,
            "annualized_volatility": annualized_volatility,
        },
    )


def build_target_portfolio(
    strategy_key: str,
    candidates: Iterable[CandidateInput],
    *,
    as_of: date,
    benchmark: CandidateInput | None = None,
    parameters: dict[str, Any] | None = None,
) -> TargetPortfolio:
    if strategy_key not in QUANT_STRATEGY_SPECS:
        raise ValueError(f"未知独立量化策略: {strategy_key}")
    resolved_parameters = {
        **QUANT_STRATEGY_SPECS[strategy_key].defaults,
        **(parameters or {}),
    }
    items = sorted(candidates, key=lambda item: item.symbol)
    from .registry import STRATEGY_MODULES

    return STRATEGY_MODULES[strategy_key].build_target(
        items,
        as_of=as_of,
        benchmark=benchmark,
        parameters=resolved_parameters,
    )
