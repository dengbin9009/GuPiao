from __future__ import annotations

from datetime import date

import pytest

from app.quant_strategies.algorithms import TargetPortfolio
from app.quant_strategies.holding_policy import HoldingContext, apply_holding_policy


def result(
    key: str,
    *,
    targets: dict[str, float],
    scores: dict[str, float],
    features: dict[str, dict] | None = None,
) -> TargetPortfolio:
    return TargetPortfolio(
        key,
        targets,
        scores,
        features or {},
        {},
    )


def holding(
    symbol: str,
    *,
    weight: float = 0.1,
    held_days: int = 1,
    close: float = 10,
    low_20d: float = 9,
    highest_close: float = 11,
    entry_atr: float = 1,
    risk_blocked: bool = False,
) -> HoldingContext:
    return HoldingContext(
        symbol=symbol,
        current_weight=weight,
        entry_date=date(2026, 7, 1),
        held_trading_days=held_days,
        latest_close=close,
        low_20d=low_20d,
        highest_close=highest_close,
        entry_atr=entry_atr,
        risk_blocked=risk_blocked,
    )


def test_short_term_reversal_always_removes_existing_holding_from_next_target():
    source = result(
        "short_term_reversal_t1",
        targets={"000001.SZ": 0.10, "000858.SZ": 0.10},
        scores={"000001.SZ": 2, "000858.SZ": 1},
    )

    adjusted = apply_holding_policy(
        source,
        holdings=[holding("000001.SZ")],
        consumed_reports=set(),
    )

    assert adjusted.target_weights == {"000858.SZ": 0.10}


def test_breakout_retains_position_until_low_or_atr_exit_is_hit():
    source = result(
        "breakout_trend",
        targets={"000858.SZ": 0.15},
        scores={"000858.SZ": 2},
    )

    retained = apply_holding_policy(
        source,
        holdings=[holding("000001.SZ", close=10, low_20d=9, highest_close=11, entry_atr=1)],
        consumed_reports=set(),
        parameters={"atr_multiple": 3.0},
    )
    exited = apply_holding_policy(
        source,
        holdings=[holding("000001.SZ", close=7.9, low_20d=8, highest_close=12, entry_atr=1)],
        consumed_reports=set(),
        parameters={"atr_multiple": 3.0},
    )

    assert retained.target_weights["000001.SZ"] == 0.10
    assert "000001.SZ" not in exited.target_weights


def test_earnings_drift_holds_twenty_days_and_rejects_consumed_report():
    source = result(
        "earnings_drift",
        targets={"000001.SZ": 0.10, "000858.SZ": 0.10},
        scores={"000001.SZ": 3, "000858.SZ": 2},
        features={
            "000001.SZ": {"report_period": "2026-06-30"},
            "000858.SZ": {"report_period": "2026-06-30"},
        },
    )

    adjusted = apply_holding_policy(
        source,
        holdings=[holding("600519.SH", held_days=19)],
        consumed_reports={("000001.SZ", "2026-06-30")},
        parameters={"holding_days": 20},
    )
    expired = apply_holding_policy(
        source,
        holdings=[holding("600519.SH", held_days=20)],
        consumed_reports={("000001.SZ", "2026-06-30")},
        parameters={"holding_days": 20},
    )

    assert "000001.SZ" not in adjusted.target_weights
    assert adjusted.target_weights["600519.SH"] == 0.10
    assert "600519.SH" not in expired.target_weights


def test_relative_strength_retains_existing_top_ten_position():
    scores = {f"000{index:03d}.SZ": float(20 - index) for index in range(1, 13)}
    targets = {symbol: 0.14 for symbol in list(scores)[:5]}
    source = result(
        "relative_strength_rotation",
        targets=targets,
        scores=scores,
    )

    adjusted = apply_holding_policy(
        source,
        holdings=[holding("000008.SZ", weight=0.08)],
        consumed_reports=set(),
    )

    assert adjusted.target_weights["000008.SZ"] == 0.08
    assert len(adjusted.target_weights) <= 5
    assert sum(adjusted.target_weights.values()) <= 0.70


def test_drifted_holdings_are_reduced_to_position_and_exposure_limits():
    source = result(
        "breakout_trend",
        targets={},
        scores={},
    )

    adjusted = apply_holding_policy(
        source,
        holdings=[
            holding(f"00000{index}.SZ", weight=0.20)
            for index in range(1, 6)
        ],
        consumed_reports=set(),
        parameters={"atr_multiple": 3.0},
    )

    assert max(adjusted.target_weights.values()) <= 0.15
    assert sum(adjusted.target_weights.values()) <= 0.60


def test_regime_policy_preserves_fifty_percent_bond_allocation():
    source = result(
        "regime_allocator",
        targets={"511010.SH": 0.50, "518880.SH": 0.20},
        scores={"511010.SH": 0.50, "518880.SH": 0.20},
    )

    adjusted = apply_holding_policy(
        source,
        holdings=[],
        consumed_reports=set(),
    )

    assert adjusted.target_weights["511010.SH"] == pytest.approx(0.50)
    assert sum(adjusted.target_weights.values()) == pytest.approx(0.70)
