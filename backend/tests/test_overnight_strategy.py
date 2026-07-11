from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo


def candidate(symbol="000001.SZ", **overrides):
    row = {
        "symbol": symbol,
        "name": "平安银行",
        "exchange": "SZSE",
        "status": "active",
        "listing_days": 1000,
        "turnover_amount": 300_000_000,
        "turnover_rate": 0.02,
        "intraday_return": 0.025,
        "above_vwap": True,
        "above_ma5": True,
        "tradable": True,
        "price": 12.5,
    }
    row.update(overrides)
    return row


def test_overnight_candidate_filters_record_rejection_reasons():
    from app.overnight_strategy import evaluate_candidates
    from app.services import OVERNIGHT_DEFAULTS

    rows = [
        candidate("920001.BJ", exchange="BSE"),
        candidate("000002.SZ", name="*ST 测试"),
        candidate("000003.SZ", turnover_amount=10_000_000),
        candidate("000004.SZ"),
    ]

    result = evaluate_candidates(rows, OVERNIGHT_DEFAULTS, critical_event_symbols={"000004.SZ"})

    assert not result.accepted
    assert {item["symbol"] for item in result.rejected} == {row["symbol"] for row in rows}
    assert all(item["reasons"] for item in result.rejected)


def test_overnight_candidate_selection_and_position_size():
    from app.overnight_strategy import calculate_position_quantity, evaluate_candidates
    from app.services import OVERNIGHT_DEFAULTS

    rows = [candidate(f"00000{i}.SZ", intraday_return=0.02 + i * 0.001) for i in range(1, 6)]
    result = evaluate_candidates(rows, OVERNIGHT_DEFAULTS, critical_event_symbols=set())
    quantity = calculate_position_quantity(equity=10_000, price=12.5, target_pct=0.20, risk_notional=2_000)

    assert len(result.accepted) == 3
    assert quantity == 100


def test_overnight_candidate_selection_orders_by_intraday_return_then_turnover():
    from app.overnight_strategy import evaluate_candidates
    from app.services import OVERNIGHT_DEFAULTS

    rows = [
        candidate("000001.SZ", intraday_return=0.031, turnover_amount=200_000_000),
        candidate("000002.SZ", intraday_return=0.038, turnover_amount=150_000_000),
        candidate("000003.SZ", intraday_return=0.038, turnover_amount=320_000_000),
    ]

    result = evaluate_candidates(rows, OVERNIGHT_DEFAULTS, critical_event_symbols=set())

    assert [item["symbol"] for item in result.accepted] == ["000003.SZ", "000002.SZ", "000001.SZ"]


def test_universe_builder_treats_change_pct_as_percentage_points():
    from app.overnight_strategy import build_universe_candidates

    stock = SimpleNamespace(
        symbol="000001.SZ",
        code="000001",
        name="平安银行",
        exchange="SZSE",
        status="active",
        last_price=10.0,
        change_pct=0.5,
        turnover_amount=200_000_000,
        quote_updated_at=None,
        created_at=None,
    )

    rows = build_universe_candidates(
        [stock],
        current=datetime(2026, 7, 10, 14, 40, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert rows[0]["intraday_return"] == 0.005
