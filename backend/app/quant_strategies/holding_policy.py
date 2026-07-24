from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from .algorithms import TargetPortfolio, _capped_weights
from .catalog import QUANT_STRATEGY_SPECS


@dataclass(frozen=True)
class HoldingContext:
    symbol: str
    current_weight: float
    entry_date: date
    held_trading_days: int
    latest_close: float
    low_20d: float
    highest_close: float
    entry_atr: float
    risk_blocked: bool = False


def apply_holding_policy(
    result: TargetPortfolio,
    *,
    holdings: Iterable[HoldingContext],
    consumed_reports: set[tuple[str, str]],
    parameters: dict[str, Any] | None = None,
) -> TargetPortfolio:
    parameters = parameters or {}
    current = {item.symbol: item for item in holdings}
    targets = dict(result.target_weights)
    rejected = dict(result.rejected)
    scores = dict(result.scores)

    if result.strategy_key == "short_term_reversal_t1":
        for symbol in current:
            targets.pop(symbol, None)
    elif result.strategy_key == "breakout_trend":
        atr_multiple = float(parameters.get("atr_multiple", 3.0))
        for symbol, item in current.items():
            exit_hit = (
                item.risk_blocked
                or item.latest_close < item.low_20d
                or item.latest_close
                <= item.highest_close - atr_multiple * max(item.entry_atr, 0)
            )
            if exit_hit:
                targets.pop(symbol, None)
            else:
                targets.setdefault(symbol, item.current_weight)
    elif result.strategy_key == "earnings_drift":
        holding_days = int(parameters.get("holding_days", 20))
        for symbol, features in result.features.items():
            report_period = str(features.get("report_period") or "")
            if report_period and (symbol, report_period) in consumed_reports:
                targets.pop(symbol, None)
                rejected[symbol] = ("同一报告期已消费",)
        for symbol, item in current.items():
            if item.risk_blocked or item.held_trading_days >= holding_days:
                targets.pop(symbol, None)
            else:
                targets.setdefault(symbol, item.current_weight)
    elif result.strategy_key == "relative_strength_rotation":
        top_ten = {
            symbol
            for symbol, _ in sorted(
                scores.items(),
                key=lambda row: (-row[1], row[0]),
            )[:10]
        }
        for symbol, item in current.items():
            if symbol in top_ten and not item.risk_blocked:
                targets.setdefault(symbol, item.current_weight)

    spec = QUANT_STRATEGY_SPECS[result.strategy_key]
    targets = {
        symbol: min(max(float(weight), 0.0), spec.max_position_pct)
        for symbol, weight in targets.items()
    }
    if len(targets) > spec.max_positions:
        retained = sorted(
            targets,
            key=lambda symbol: (
                0 if symbol in current else 1,
                -scores.get(symbol, 0),
                symbol,
            ),
        )[: spec.max_positions]
        targets = {symbol: targets[symbol] for symbol in retained}
    exposure = sum(targets.values())
    if exposure > spec.max_total_exposure_pct:
        held_symbols = [symbol for symbol in targets if symbol in current]
        held_exposure = sum(targets[symbol] for symbol in held_symbols)
        if held_exposure > spec.max_total_exposure_pct:
            scale = spec.max_total_exposure_pct / held_exposure
            targets = {
                symbol: targets[symbol] * scale
                for symbol in held_symbols
            }
            held_exposure = spec.max_total_exposure_pct
        remaining = max(spec.max_total_exposure_pct - held_exposure, 0)
        new_scores = {
            symbol: max(scores.get(symbol, targets[symbol]), 1e-9)
            for symbol in targets
            if symbol not in current
        }
        resized = _capped_weights(
            new_scores,
            total=remaining,
            cap=spec.max_position_pct,
        )
        targets = {
            **{symbol: targets[symbol] for symbol in held_symbols},
            **resized,
        }

    return TargetPortfolio(
        result.strategy_key,
        dict(sorted(targets.items())),
        scores,
        result.features,
        rejected,
        result.exit_after_trading_days,
        result.metadata,
    )
