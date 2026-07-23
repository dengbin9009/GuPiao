from __future__ import annotations

import pytest

from app.probability_portfolio.allocation import (
    AllocationCandidate,
    allocate_portfolio,
    plan_buy_quantity,
)


def candidate(
    symbol: str,
    *,
    probability: float,
    expected_return: float = 0.02,
    volatility: float = 0.10,
) -> AllocationCandidate:
    return AllocationCandidate(
        stock_id=int(symbol[:6]),
        symbol=f"{symbol}.SZ",
        probability=probability,
        expected_net_return=expected_return,
        volatility_20d=volatility,
    )


def test_allocation_trades_fewer_than_ten_qualified_candidates():
    result = allocate_portfolio(
        [
            candidate("000001", probability=0.62),
            candidate("000002", probability=0.59),
            candidate("000003", probability=0.57),
        ]
    )

    assert len(result.allocations) == 3
    assert {item.symbol for item in result.allocations} == {
        "000001.SZ",
        "000002.SZ",
        "000003.SZ",
    }
    assert result.total_weight <= 0.60
    assert all(0.02 <= item.target_weight <= 0.36 for item in result.allocations)


def test_allocation_selects_at_most_ten_and_is_not_equal_weight():
    rows = [
        candidate(
            f"{index:06d}",
            probability=0.55 + index / 1000,
            expected_return=0.01 + index / 1000,
            volatility=0.08 + index / 1000,
        )
        for index in range(1, 13)
    ]

    result = allocate_portfolio(rows)

    assert len(result.allocations) == 10
    assert len({round(item.target_weight, 8) for item in result.allocations}) > 1
    assert result.allocations[0].symbol == "000012.SZ"
    assert sum(item.target_weight for item in result.allocations) <= 0.60 + 1e-9


def test_allocation_caps_a_high_confidence_single_stock_at_36_percent():
    result = allocate_portfolio(
        [candidate("000001", probability=0.78, expected_return=0.05, volatility=0.05)]
    )

    assert len(result.allocations) == 1
    assert result.allocations[0].target_weight == pytest.approx(0.36)
    assert result.total_weight == pytest.approx(0.36)


def test_allocation_rejects_below_threshold_and_negative_expectation():
    result = allocate_portfolio(
        [
            candidate("000001", probability=0.549),
            candidate("000002", probability=0.60, expected_return=0),
            candidate("000003", probability=0.65, expected_return=-0.01),
        ]
    )

    assert result.allocations == []
    assert {item.symbol for item in result.rejected} == {
        "000001.SZ",
        "000002.SZ",
        "000003.SZ",
    }


def test_equal_risk_and_return_gives_higher_probability_more_weight():
    result = allocate_portfolio(
        [
            candidate("000001", probability=0.56),
            candidate("000002", probability=0.64),
        ]
    )

    weights = {item.symbol: item.target_weight for item in result.allocations}
    assert weights["000002.SZ"] > weights["000001.SZ"]


def test_quantity_uses_slippage_fees_cash_and_board_lots():
    planned = plan_buy_quantity(
        total_asset=2_000_000,
        available_cash=240_000,
        target_weight=0.12,
        market_price=10,
        slippage_bps=5,
        commission_rate=0.0003,
        min_commission=5,
        transfer_fee_rate=0,
    )

    assert planned.quantity % 100 == 0
    assert planned.quantity == 23_900
    assert planned.total_cost <= 240_000
    assert planned.fill_price == pytest.approx(10.005)

