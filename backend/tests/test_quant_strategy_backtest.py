from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import (
    BacktestRun,
    FinancialReportSnapshot,
    MarketDailyBar,
    MarketDailyMetric,
    Stock,
    StockEvent,
    StrategyBacktestQualification,
    StrategyConfig,
    StrategyDefinition,
)
from app.quant_strategies.backtest import (
    BacktestMetrics,
    _PointInTimeCache,
    _build_cached_historical_target_portfolio,
    _schedule_matches,
    build_historical_target_portfolio,
    qualification_passes,
    run_quant_backtest,
)
from app.quant_strategies.algorithms import TargetPortfolio
from app.quant_strategies.runtime import seed_quant_strategy_runtimes
from app.services import seed_database


def test_qualification_requires_all_five_gates():
    passing = BacktestMetrics(
        trading_days=500,
        data_completeness=0.98,
        annualized_return=0.01,
        sharpe_ratio=0.30,
        max_drawdown=-0.25,
        trade_count=30,
        final_equity=2_020_000,
        equity_curve=(),
    )

    assert qualification_passes(passing)
    for values in (
        {"trading_days": 499},
        {"data_completeness": 0.979},
        {"annualized_return": 0.0},
        {"sharpe_ratio": 0.29},
        {"max_drawdown": -0.251},
        {"trade_count": 29},
    ):
        changed = BacktestMetrics(
            **{**passing.__dict__, **values},
        )
        assert not qualification_passes(changed)


def test_backtest_monthly_schedule_matches_runtime_month_end_rule():
    dates = ["2026-07-30", "2026-07-31", "2026-08-03", "2026-08-04"]

    assert not _schedule_matches("monthly", dates, 0)
    assert _schedule_matches("monthly", dates, 1)
    assert not _schedule_matches("monthly", dates, 2)


def test_monthly_backtest_trades_only_after_month_end_signals(tmp_path: Path):
    engine, _config_id, start = setup_backtest_db(tmp_path)
    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig)
            .join(StrategyDefinition)
            .where(StrategyDefinition.key == "multi_factor_core")
        )
        metrics, _qualification = run_quant_backtest(
            db,
            config,
            start_date=start.isoformat(),
            end_date=(start + timedelta(days=119)).isoformat(),
            portfolio_builder=lambda *_args, **_kwargs: {"000858.SZ": 0.10},
        )

        rebalanced = [
            row for row in metrics.equity_curve if row["rebalance_applied"]
        ]
        assert 3 <= len(rebalanced) <= 4
        assert metrics.trade_count <= len(rebalanced)


def test_short_reversal_backtest_applies_same_next_day_exit_policy(
    tmp_path: Path,
    monkeypatch,
):
    engine, _config_id, start = setup_backtest_db(tmp_path)

    def raw_portfolio(_cache, _config, _definition, *, as_of):
        return TargetPortfolio(
            "short_term_reversal_t1",
            {"000858.SZ": 0.10},
            {"000858.SZ": 1.0},
            {"000858.SZ": {}},
            {},
            exit_after_trading_days=1,
        )

    monkeypatch.setattr(
        "app.quant_strategies.backtest._build_cached_historical_target_portfolio",
        raw_portfolio,
    )
    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig)
            .join(StrategyDefinition)
            .where(StrategyDefinition.key == "short_term_reversal_t1")
        )
        metrics, _qualification = run_quant_backtest(
            db,
            config,
            start_date=start.isoformat(),
            end_date=(start + timedelta(days=9)).isoformat(),
        )

        assert metrics.trade_count >= 6


def setup_backtest_db(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'backtest.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(database_url=database_url, live_enabled=False, broker_adapter="simulation")
    with Session(engine) as db:
        seed_database(db, settings)
        configs = seed_quant_strategy_runtimes(db, settings)
        config_id = configs["relative_strength_rotation"].id
        start = date(2024, 1, 1)
        stocks = list(
            db.scalars(
                select(Stock)
                .where(Stock.symbol.in_(["000001.SZ", "000858.SZ"]))
                .order_by(Stock.symbol)
            )
        )
        for index, stock in enumerate(stocks, start=1):
            stock.listing_date = "2010-01-01"
            stock.instrument_type = "STOCK"
            price = 8 + index
            for offset in range(560):
                day = start + timedelta(days=offset)
                price *= 1.0005 + index * 0.0003
                db.add(
                    MarketDailyBar(
                        stock_id=stock.id,
                        trade_date=day.isoformat(),
                        open=price * 0.999,
                        high=price * 1.01,
                        low=price * 0.99,
                        close=price,
                        adjusted_close=price,
                        adjustment_factor=1,
                        volume=20_000_000,
                        amount=200_000_000,
                        source="point-in-time-test",
                    )
                )
        db.commit()
    return engine, config_id, start


def test_backtest_uses_next_day_open_and_persists_qualification(tmp_path: Path, monkeypatch):
    engine, config_id, start = setup_backtest_db(tmp_path)
    observed = []

    def fake_portfolio(_db, config, *, as_of):
        observed.append(as_of)
        return {"000858.SZ": 0.20}

    with Session(engine) as db:
        config = db.get(StrategyConfig, config_id)
        metrics, qualification = run_quant_backtest(
            db,
            config,
            start_date=start.isoformat(),
            end_date=(start + timedelta(days=559)).isoformat(),
            portfolio_builder=fake_portfolio,
        )

        assert metrics.trading_days >= 500
        assert metrics.trade_count > 0
        assert observed
        assert max(observed) < start + timedelta(days=559)
        assert qualification.id is not None
        assert qualification.trading_days == metrics.trading_days
        assert qualification.data_completeness == metrics.data_completeness
        assert db.get(StrategyBacktestQualification, qualification.id) is not None
        persisted = db.get(BacktestRun, qualification.backtest_run_id)
        assert persisted.status == "completed"
        assert persisted.data_provider == "point_in_time_real"
        assert persisted.metrics["precision"] == "next_day_open"
        assert persisted.metrics["out_of_sample_start_date"]
        assert persisted.metrics["equity_curve"] == list(metrics.equity_curve)
        assert metrics.equity_curve[0]["precision"] == "next_day_open"


def test_backtest_rejects_demo_or_future_rows(tmp_path: Path):
    engine, config_id, start = setup_backtest_db(tmp_path)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_id)
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        db.add(
            MarketDailyBar(
                stock_id=stock.id,
                trade_date=(start + timedelta(days=600)).isoformat(),
                open=999,
                high=999,
                low=999,
                close=999,
                adjusted_close=999,
                volume=1,
                amount=1,
                source="demo",
            )
        )
        db.commit()

        with pytest.raises(ValueError, match="演示数据"):
            run_quant_backtest(
                db,
                config,
                start_date=start.isoformat(),
                end_date=(start + timedelta(days=620)).isoformat(),
            )


def test_backtest_carries_last_close_for_missing_held_open_without_trading(
    tmp_path: Path,
):
    engine, _config_id, start = setup_backtest_db(tmp_path)
    missing_date = (start + timedelta(days=4)).isoformat()
    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig)
            .join(StrategyDefinition)
            .where(StrategyDefinition.key == "breakout_trend")
        )
        stock = db.scalar(select(Stock).where(Stock.symbol == "000858.SZ"))
        missing = db.scalar(
            select(MarketDailyBar).where(
                MarketDailyBar.stock_id == stock.id,
                MarketDailyBar.trade_date == missing_date,
            )
        )
        db.delete(missing)
        db.commit()

        metrics, _qualification = run_quant_backtest(
            db,
            config,
            start_date=start.isoformat(),
            end_date=(start + timedelta(days=9)).isoformat(),
            portfolio_builder=lambda *_args, **_kwargs: {stock.symbol: 0.10},
        )

        missing_day = next(
            row for row in metrics.equity_curve if row["trade_date"] == missing_date
        )
        assert missing_day["precision"] == "carried_last_close"
        assert missing_day["rebalance_applied"] is False
        assert missing_day["equity"] > 1_900_000
        assert metrics.data_completeness < 1


def test_default_backtest_preloads_point_in_time_inputs_with_constant_queries(
    tmp_path: Path,
):
    engine, _config_id, start = setup_backtest_db(tmp_path)
    select_statements: list[str] = []

    def capture_select(_conn, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT"):
            select_statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_select)
    try:
        with Session(engine) as db:
            config = db.scalar(
                select(StrategyConfig)
                .join(StrategyDefinition)
                .where(StrategyDefinition.key == "breakout_trend")
            )
            select_statements.clear()

            metrics, _qualification = run_quant_backtest(
                db,
                config,
                start_date=start.isoformat(),
                end_date=(start + timedelta(days=59)).isoformat(),
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture_select)

    assert metrics.trading_days == 59
    assert len(select_statements) <= 20


def test_backtest_point_in_time_cache_never_exposes_future_inputs(tmp_path: Path):
    engine, _config_id, start = setup_backtest_db(tmp_path)
    before = start + timedelta(days=5)
    future = start + timedelta(days=10)
    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig)
            .join(StrategyDefinition)
            .where(StrategyDefinition.key == "breakout_trend")
        )
        definition = db.get(StrategyDefinition, config.strategy_definition_id)
        stock = db.scalar(select(Stock).where(Stock.symbol == "000858.SZ"))
        db.add_all(
            [
                MarketDailyMetric(
                    stock_id=stock.id,
                    trade_date=start.isoformat(),
                    pb=1.0,
                    source="point-in-time-test",
                    captured_at=datetime.combine(start, time(16, 15)),
                ),
                MarketDailyMetric(
                    stock_id=stock.id,
                    trade_date=future.isoformat(),
                    pb=99.0,
                    source="point-in-time-test",
                    captured_at=datetime.combine(future, time(16, 15)),
                ),
                FinancialReportSnapshot(
                    stock_id=stock.id,
                    report_period="2023-09-30",
                    announcement_date=start.isoformat(),
                    actual_announcement_date=start.isoformat(),
                    available_on=start.isoformat(),
                    roe=0.1,
                    source="point-in-time-test",
                ),
                FinancialReportSnapshot(
                    stock_id=stock.id,
                    report_period="2023-12-31",
                    announcement_date=future.isoformat(),
                    actual_announcement_date=future.isoformat(),
                    available_on=future.isoformat(),
                    roe=0.9,
                    source="point-in-time-test",
                ),
                StockEvent(
                    stock_id=stock.id,
                    event_type="major_announcement",
                    title="未来风险公告",
                    source="point-in-time-test",
                    source_event_id="future-risk-event",
                    published_at=datetime.combine(future, time(16, 0)),
                    fetched_at=datetime.combine(future, time(16, 5)),
                ),
            ]
        )
        db.commit()

        cache = _PointInTimeCache.load(
            db,
            config,
            definition,
            end_date=(future + timedelta(days=1)).isoformat(),
            candidate_stocks={stock.symbol: stock},
        )
        candidate = cache.candidate(stock, before)

        assert candidate.bars[-1].trade_date == before
        assert candidate.metric["pb"] == 1.0
        assert candidate.financial.roe == 0.1
        assert candidate.financial_history == ()
        assert not cache.has_blocking_event(
            stock.id,
            cutoff=datetime.combine(before, time(16, 30)),
        )
        assert cache.candidate(stock, future).metric["pb"] == 99.0
        assert cache.has_blocking_event(
            stock.id,
            cutoff=datetime.combine(future, time(16, 30)),
        )


@pytest.mark.parametrize(
    "strategy_key",
    [
        "multi_factor_core",
        "relative_strength_rotation",
        "breakout_trend",
        "short_term_reversal_t1",
        "low_vol_quality",
        "earnings_drift",
    ],
)
def test_cached_backtest_portfolio_matches_point_in_time_queries(
    tmp_path: Path,
    strategy_key: str,
):
    engine, _config_id, start = setup_backtest_db(tmp_path)
    as_of = start + timedelta(days=300)
    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig)
            .join(StrategyDefinition)
            .where(StrategyDefinition.key == strategy_key)
        )
        definition = db.get(StrategyDefinition, config.strategy_definition_id)
        stocks = {
            stock.symbol: stock
            for stock in db.scalars(
                select(Stock).where(
                    Stock.status == "active",
                    Stock.instrument_type == "STOCK",
                )
            )
        }
        cache = _PointInTimeCache.load(
            db,
            config,
            definition,
            end_date=as_of.isoformat(),
            candidate_stocks=stocks,
        )

        queried = build_historical_target_portfolio(db, config, as_of=as_of)
        cached = _build_cached_historical_target_portfolio(
            cache,
            config,
            definition,
            as_of=as_of,
        )

        assert cached == queried


def test_backtest_point_in_time_cache_excludes_beijing_exchange_stocks(
    tmp_path: Path,
):
    engine, _config_id, start = setup_backtest_db(tmp_path)
    as_of = start + timedelta(days=30)
    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig)
            .join(StrategyDefinition)
            .where(StrategyDefinition.key == "breakout_trend")
        )
        definition = db.get(StrategyDefinition, config.strategy_definition_id)
        beijing = Stock(
            code="430001",
            exchange="BSE",
            symbol="430001.BJ",
            name="北交所测试股",
            status="active",
            instrument_type="STOCK",
            listing_date="2010-01-01",
        )
        db.add(beijing)
        db.flush()
        for offset in range(31):
            day = start + timedelta(days=offset)
            db.add(
                MarketDailyBar(
                    stock_id=beijing.id,
                    trade_date=day.isoformat(),
                    open=10,
                    high=11,
                    low=9,
                    close=10,
                    adjusted_close=10,
                    adjustment_factor=1,
                    volume=20_000_000,
                    amount=300_000_000,
                    source="point-in-time-test",
                )
            )
        db.commit()
        stocks = {
            stock.symbol: stock
            for stock in db.scalars(
                select(Stock).where(
                    Stock.status == "active",
                    Stock.instrument_type == "STOCK",
                )
            )
        }
        cache = _PointInTimeCache.load(
            db,
            config,
            definition,
            end_date=as_of.isoformat(),
            candidate_stocks=stocks,
        )

        selected, rejected = cache.universe(
            config,
            definition.key,
            as_of,
            decision_at=datetime.combine(as_of, time(16, 30)),
        )

        assert beijing.symbol not in {stock.symbol for stock in selected}
        assert beijing.symbol not in rejected
