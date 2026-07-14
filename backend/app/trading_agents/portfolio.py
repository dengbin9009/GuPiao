from __future__ import annotations

from typing import Any


class PortfolioMappingError(ValueError):
    pass


RATING_TARGETS = {
    "Buy": 0.20,
    "Overweight": 0.10,
}


def _scale_to_exposure(
    targets: dict[str, float], *, max_total_exposure_pct: float
) -> dict[str, float]:
    total = sum(targets.values())
    if total <= max_total_exposure_pct or total <= 0:
        return targets
    factor = max_total_exposure_pct / total
    return {symbol: weight * factor for symbol, weight in targets.items()}


def map_target_weights(
    *,
    analyses: list[dict[str, Any]],
    current_weights: dict[str, float],
    mode: str,
    max_positions: int,
    max_position_pct: float,
    max_total_exposure_pct: float,
) -> dict[str, float]:
    ordered = sorted(
        analyses,
        key=lambda item: (int(item.get("rank", 9999)), str(item.get("symbol", ""))),
    )
    if mode == "ai_target":
        targets: dict[str, float] = {}
        for item in ordered:
            value = item.get("ai_target_weight")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise PortfolioMappingError("AI目标仓位缺失或无效")
            weight = float(value)
            if not 0 <= weight <= max_position_pct:
                raise PortfolioMappingError("AI目标仓位超出单股上限")
            if weight > 0:
                targets[str(item["symbol"])] = weight
        if len(targets) > max_positions:
            raise PortfolioMappingError("AI目标组合超过最大持仓数")
        if sum(targets.values()) > max_total_exposure_pct + 1e-9:
            raise PortfolioMappingError("AI目标组合超过总仓位上限")
        return targets

    targets: dict[str, float] = {}
    eligible: list[dict[str, Any]] = []
    for item in ordered:
        symbol = str(item.get("symbol", ""))
        rating = str(item.get("rating", ""))
        if rating == "Hold":
            weight = min(max_position_pct, float(current_weights.get(symbol, 0)))
            if weight > 0:
                targets[symbol] = weight
        elif rating == "Underweight":
            if mode == "equal_weight":
                continue
            weight = min(max_position_pct, float(current_weights.get(symbol, 0)) / 2)
            if weight > 0:
                targets[symbol] = weight
        elif rating == "Sell":
            continue
        elif rating in {"Buy", "Overweight"}:
            eligible.append(item)
        else:
            raise PortfolioMappingError(f"未知评级: {rating}")

    if len(targets) > max_positions:
        ordered_target_symbols = [
            str(item["symbol"])
            for item in ordered
            if str(item.get("symbol", "")) in targets
        ]
        keep = set(ordered_target_symbols[:max_positions])
        targets = {
            symbol: weight
            for symbol, weight in targets.items()
            if symbol in keep
        }
    slots = max(0, max_positions - len(targets))
    eligible = [item for item in eligible if str(item["symbol"]) not in targets][:slots]
    if mode == "fixed_rating":
        for item in eligible:
            targets[str(item["symbol"])] = min(
                max_position_pct,
                RATING_TARGETS[str(item["rating"])],
            )
    elif mode == "equal_weight":
        remaining = max(0.0, max_total_exposure_pct - sum(targets.values()))
        equal = min(max_position_pct, remaining / len(eligible)) if eligible else 0
        for item in eligible:
            if equal > 0:
                targets[str(item["symbol"])] = equal
    else:
        raise PortfolioMappingError(f"未知仓位映射模式: {mode}")

    targets = _scale_to_exposure(
        targets,
        max_total_exposure_pct=max_total_exposure_pct,
    )
    return {
        symbol: weight
        for symbol, weight in targets.items()
        if weight > 1e-12
    }
