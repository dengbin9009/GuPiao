from __future__ import annotations

from datetime import datetime, timedelta


def sample_bars():
    start = datetime(2026, 6, 20, 14, 40)
    rows = []
    prices = [10.0, 10.05, 10.12, 10.18, 10.25, 10.30]
    for index, price in enumerate(prices):
        rows.append(
            {
                "timestamp": (start + timedelta(minutes=index)).isoformat(),
                "open": price - 0.02,
                "high": price + 0.03,
                "low": price - 0.03,
                "close": price,
                "volume": 100000 + index * 1000,
                "amount": price * (100000 + index * 1000),
            }
        )
    return rows


def test_backtest_engine_runs_and_returns_metrics():
    from app.backtest_engine import BacktestEngine, BacktestRequest

    engine = BacktestEngine()
    result = engine.run(
        BacktestRequest(
            symbol="000001.SZ",
            bars=sample_bars(),
            initial_cash=10000,
            commission_rate=0.0003,
            min_commission=5,
            stamp_tax_rate=0.0005,
            transfer_fee_rate=0.0,
            slippage_bps=5,
            buy_index=1,
            sell_index=4,
            quantity=100,
        )
    )

    assert len(result.trades) == 2
    assert result.metrics["trade_count"] == 2
    assert "cumulative_return" in result.metrics
    assert result.equity_curve


def test_backtest_engine_respects_lot_and_cost_constraints():
    from app.backtest_engine import BacktestEngine, BacktestRequest

    engine = BacktestEngine()
    result = engine.run(
        BacktestRequest(
            symbol="000001.SZ",
            bars=sample_bars(),
            initial_cash=10000,
            commission_rate=0.0003,
            min_commission=5,
            stamp_tax_rate=0.0005,
            transfer_fee_rate=0.0,
            slippage_bps=5,
            buy_index=1,
            sell_index=4,
            quantity=150,
        )
    )

    assert result.trades[0]["quantity"] == 100
    assert result.trades[0]["commission"] >= 5


def test_backtest_engine_rejects_missing_bar_window():
    from app.backtest_engine import BacktestEngine, BacktestRequest

    engine = BacktestEngine()

    try:
        engine.run(
            BacktestRequest(
                symbol="000001.SZ",
                bars=sample_bars()[:2],
                initial_cash=10000,
                commission_rate=0.0003,
                min_commission=5,
                stamp_tax_rate=0.0005,
                transfer_fee_rate=0.0,
                slippage_bps=5,
                buy_index=1,
                sell_index=4,
                quantity=100,
            )
        )
    except ValueError as exc:
        assert "回测数据不足" in str(exc)
    else:
        raise AssertionError("expected ValueError for insufficient bars")
