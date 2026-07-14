from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.trading_agents.portfolio import PortfolioMappingError, map_target_weights
from app.trading_agents.prefilter import build_snapshot, select_candidates
from app.trading_agents.config import TRADING_AGENTS_DEFAULTS, configuration_fingerprint


SHANGHAI = ZoneInfo("Asia/Shanghai")


def candidate(symbol: str, *, momentum: float, amount: float, holding: bool = False):
    start = datetime(2026, 3, 1, tzinfo=SHANGHAI)
    bars = []
    price = 10.0
    daily = (1 + momentum) ** (1 / 59)
    for index in range(60):
        price *= daily
        bars.append(
            {
                "trade_date": (start + timedelta(days=index)).date().isoformat(),
                "close": price,
                "amount": amount,
            }
        )
    return {
        "symbol": symbol,
        "name": symbol,
        "exchange": "SSE",
        "status": "active",
        "last_price": price,
        "turnover_amount": amount,
        "quote_updated_at": "2026-07-13T13:25:00+08:00",
        "bars": bars,
        "is_holding": holding,
    }


def test_prefilter_is_deterministic_and_keeps_holdings_outside_top_n():
    rows = [
        candidate(f"6000{index:02d}.SH", momentum=0.05 + index / 100, amount=2e8 + index)
        for index in range(12)
    ]
    held = candidate("601999.SH", momentum=-0.05, amount=5e7, holding=True)
    rows.append(held)

    result = select_candidates(
        rows,
        as_of="2026-07-13",
        prefilter_size=100,
        top_n=10,
        critical_event_symbols=set(),
    )

    assert len(result.candidates) == 10
    assert result.candidates[0]["symbol"] == "600011.SH"
    assert result.holdings == ["601999.SH"]
    assert result.required_symbols[-1] == "601999.SH"
    assert len(result.required_symbols) == 11


def test_prefilter_rejects_future_or_incomplete_daily_bars():
    incomplete = candidate("600001.SH", momentum=0.2, amount=3e8)
    incomplete["bars"] = incomplete["bars"][:59]
    future = candidate("600002.SH", momentum=0.3, amount=4e8)
    future["bars"][-1]["trade_date"] = "2026-07-14"

    result = select_candidates(
        [incomplete, future],
        as_of="2026-07-13",
        prefilter_size=100,
        top_n=10,
        critical_event_symbols=set(),
    )

    assert result.candidates == []
    assert result.rejected["600001.SH"] == "日线不足60根"
    assert result.rejected["600002.SH"] == "日线包含未来数据"


def test_snapshot_hash_is_stable_and_sensitive_to_content():
    one = build_snapshot({"symbols": ["600001.SH"], "price": 10.0})
    two = build_snapshot({"price": 10.0, "symbols": ["600001.SH"]})
    changed = build_snapshot({"symbols": ["600001.SH"], "price": 10.1})
    assert one.sha256 == two.sha256
    assert one.payload == two.payload
    assert one.sha256 != changed.sha256


def test_fixed_rating_mapping_obeys_position_and_exposure_caps():
    result = map_target_weights(
        analyses=[
            {"symbol": f"60000{i}.SH", "rating": "Buy", "rank": i}
            for i in range(1, 6)
        ],
        current_weights={},
        mode="fixed_rating",
        max_positions=5,
        max_position_pct=0.2,
        max_total_exposure_pct=0.6,
    )
    assert len(result) == 5
    assert sum(result.values()) == pytest.approx(0.6)
    assert max(result.values()) <= 0.2


def test_rating_mapping_supports_hold_underweight_sell_and_ai_validation():
    current = {"600001.SH": 0.16, "600002.SH": 0.10, "600003.SH": 0.08}
    fixed = map_target_weights(
        analyses=[
            {"symbol": "600001.SH", "rating": "Hold", "rank": 1},
            {"symbol": "600002.SH", "rating": "Underweight", "rank": 2},
            {"symbol": "600003.SH", "rating": "Sell", "rank": 3},
        ],
        current_weights=current,
        mode="fixed_rating",
        max_positions=5,
        max_position_pct=0.2,
        max_total_exposure_pct=0.6,
    )
    assert fixed == {"600001.SH": 0.16, "600002.SH": 0.05}

    with pytest.raises(PortfolioMappingError, match="AI目标仓位"):
        map_target_weights(
            analyses=[{"symbol": "600001.SH", "rating": "Buy", "rank": 1}],
            current_weights={},
            mode="ai_target",
            max_positions=5,
            max_position_pct=0.2,
            max_total_exposure_pct=0.6,
        )


def test_ai_target_mapping_rejects_portfolios_beyond_hard_caps():
    with pytest.raises(PortfolioMappingError, match="最大持仓"):
        map_target_weights(
            analyses=[
                {
                    "symbol": f"60000{index}.SH",
                    "rating": "Buy",
                    "rank": index,
                    "ai_target_weight": 0.1,
                }
                for index in range(1, 7)
            ],
            current_weights={},
            mode="ai_target",
            max_positions=5,
            max_position_pct=0.2,
            max_total_exposure_pct=0.6,
        )

    with pytest.raises(PortfolioMappingError, match="总仓位"):
        map_target_weights(
            analyses=[
                {
                    "symbol": f"60000{index}.SH",
                    "rating": "Buy",
                    "rank": index,
                    "ai_target_weight": 0.2,
                }
                for index in range(1, 5)
            ],
            current_weights={},
            mode="ai_target",
            max_positions=5,
            max_position_pct=0.2,
            max_total_exposure_pct=0.6,
        )


def test_equal_weight_mapping_allocates_at_most_five_positions():
    result = map_target_weights(
        analyses=[
            {"symbol": f"60000{i}.SH", "rating": "Overweight", "rank": i}
            for i in range(1, 8)
        ],
        current_weights={},
        mode="equal_weight",
        max_positions=5,
        max_position_pct=0.2,
        max_total_exposure_pct=0.6,
    )
    assert len(result) == 5
    assert all(weight == pytest.approx(0.12) for weight in result.values())
    assert sum(result.values()) == pytest.approx(0.60)


def test_equal_weight_clears_underweight_and_sell_ratings():
    result = map_target_weights(
        analyses=[
            {"symbol": "600001.SH", "rating": "Hold", "rank": 1},
            {"symbol": "600002.SH", "rating": "Underweight", "rank": 2},
            {"symbol": "600003.SH", "rating": "Sell", "rank": 3},
            {"symbol": "600004.SH", "rating": "Buy", "rank": 4},
        ],
        current_weights={
            "600001.SH": 0.10,
            "600002.SH": 0.10,
            "600003.SH": 0.10,
        },
        mode="equal_weight",
        max_positions=5,
        max_position_pct=0.2,
        max_total_exposure_pct=0.6,
    )

    assert result == {"600001.SH": 0.10, "600004.SH": 0.20}


def test_configuration_fingerprint_ignores_only_dry_run_switch():
    enabled = {**TRADING_AGENTS_DEFAULTS, "dry_run": True}
    disabled = {**TRADING_AGENTS_DEFAULTS, "dry_run": False}
    changed_model = {**disabled, "deep_model": "gpt-5.4"}

    assert configuration_fingerprint(enabled, simulation_account_id=2) == (
        configuration_fingerprint(disabled, simulation_account_id=2)
    )
    assert configuration_fingerprint(disabled, simulation_account_id=2) != (
        configuration_fingerprint(changed_model, simulation_account_id=2)
    )
    assert configuration_fingerprint(disabled, simulation_account_id=2) != (
        configuration_fingerprint(disabled, simulation_account_id=3)
    )


def test_hold_ratings_cannot_preserve_more_than_max_positions():
    analyses = [
        {"symbol": f"60000{i}.SH", "rating": "Hold", "rank": i}
        for i in range(1, 8)
    ]
    current_weights = {item["symbol"]: 0.08 for item in analyses}

    result = map_target_weights(
        analyses=analyses,
        current_weights=current_weights,
        mode="fixed_rating",
        max_positions=5,
        max_position_pct=0.2,
        max_total_exposure_pct=0.6,
    )

    assert list(result) == [f"60000{i}.SH" for i in range(1, 6)]
