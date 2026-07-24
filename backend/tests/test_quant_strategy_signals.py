from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import (
    DataSourceState,
    FinancialReportSnapshot,
    MarketDailyBar,
    MarketDailyMetric,
    QuantCandidateScore,
    Position,
    SimulationAccount,
    Stock,
    StockEvent,
    StrategyConfig,
)
from app.quant_strategies.runtime import seed_quant_strategy_runtimes
from app.quant_strategies.signals import DataNotReadyError, build_signal_decision
from app.quant_strategies.signals import _financial_rows, _universe
from app.services import seed_database


SHANGHAI = ZoneInfo("Asia/Shanghai")


def seed_signal_data(db: Session, as_of: date) -> list[Stock]:
    stocks = []
    start = as_of - timedelta(days=269)
    for stock_index, symbol in enumerate(("000001.SZ", "000858.SZ"), start=1):
        stock = db.scalar(select(Stock).where(Stock.symbol == symbol))
        stock.listing_date = (start - timedelta(days=400)).isoformat()
        stock.instrument_type = "STOCK"
        stocks.append(stock)
        price = 10 + stock_index
        for day in range(270):
            trade_date = start + timedelta(days=day)
            price *= 1.002 + stock_index * 0.0001
            db.add(
                MarketDailyBar(
                    stock_id=stock.id,
                    trade_date=trade_date.isoformat(),
                    open=price * 0.99,
                    high=price * 1.01,
                    low=price * 0.98,
                    close=price,
                    adjusted_close=price,
                    adjustment_factor=1,
                    volume=20_000_000,
                    amount=200_000_000,
                    source="test-real",
                )
            )
        db.add(
            MarketDailyMetric(
                stock_id=stock.id,
                trade_date=as_of.isoformat(),
                pe_ttm=8 + stock_index,
                pb=0.8 + stock_index / 10,
                source="test-real",
            )
        )
        db.add(
            FinancialReportSnapshot(
                stock_id=stock.id,
                report_period=(as_of - timedelta(days=90)).isoformat(),
                announcement_date=(as_of - timedelta(days=10)).isoformat(),
                actual_announcement_date=(as_of - timedelta(days=10)).isoformat(),
                available_on=(as_of - timedelta(days=9)).isoformat(),
                eps=1.0 + stock_index / 10,
                roe=0.15 + stock_index / 100,
                gross_margin=0.30 + stock_index / 100,
                operating_cash_flow=20,
                total_assets=100,
                total_liabilities=30,
                source="test-real",
            )
        )
    db.commit()
    return stocks


def setup_database(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'signals.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(database_url=database_url, live_enabled=False, broker_adapter="simulation")
    with Session(engine) as db:
        seed_database(db, settings)
        configs = seed_quant_strategy_runtimes(db, settings)
        event_source = db.scalar(
            select(DataSourceState).where(
                DataSourceState.provider == "akshare_events"
            )
        )
        event_source.healthy = True
        event_source.last_checked_at = datetime(
            2026, 7, 23, 16, 25, tzinfo=SHANGHAI
        )
        for provider in (
            "quant_stock_daily",
            "quant_daily_metric",
            "quant_financial",
        ):
            db.add(
                DataSourceState(
                    provider=provider,
                    enabled=True,
                    healthy=True,
                    capabilities=[provider],
                    last_checked_at=datetime(
                        2026, 7, 23, 16, 20, tzinfo=SHANGHAI
                    ),
                    stale_after_seconds=86400,
                )
            )
        db.commit()
        config_id = configs["multi_factor_core"].id
    return engine, config_id


def test_signal_fails_closed_when_corporate_event_source_is_stale(
    tmp_path: Path,
):
    engine, config_id = setup_database(tmp_path)
    as_of = date(2026, 7, 23)
    current = datetime(2026, 7, 23, 16, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        seed_signal_data(db, as_of)
        event_source = db.scalar(
            select(DataSourceState).where(
                DataSourceState.provider == "akshare_events"
            )
        )
        event_source.last_checked_at = current - timedelta(
            seconds=event_source.stale_after_seconds + 1
        )
        db.commit()
        config = db.get(StrategyConfig, config_id)

        with pytest.raises(DataNotReadyError, match="风险公告数据已过期"):
            build_signal_decision(db, config, current=current)


def test_signal_fails_closed_until_same_day_quant_batch_is_complete(
    tmp_path: Path,
):
    engine, config_id = setup_database(tmp_path)
    as_of = date(2026, 7, 23)
    current = datetime(2026, 7, 23, 16, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        seed_signal_data(db, as_of)
        state = db.scalar(
            select(DataSourceState).where(
                DataSourceState.provider == "quant_stock_daily"
            )
        )
        state.healthy = False
        state.last_error = "同步进行中"
        db.commit()
        config = db.get(StrategyConfig, config_id)

        with pytest.raises(DataNotReadyError, match="股票日线与复权批次"):
            build_signal_decision(db, config, current=current)


def test_signal_uses_only_recent_events_published_before_decision_time(
    tmp_path: Path,
):
    engine, config_id = setup_database(tmp_path)
    as_of = date(2026, 7, 23)
    current = datetime(2026, 7, 23, 16, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        stocks = seed_signal_data(db, as_of)
        db.add_all(
            [
                StockEvent(
                    stock_id=stocks[0].id,
                    event_type="major_announcement",
                    severity="warning",
                    title="八天前的历史公告",
                    source="test",
                    source_event_id="old-event",
                    published_at=current - timedelta(days=8),
                ),
                StockEvent(
                    stock_id=stocks[1].id,
                    event_type="major_announcement",
                    severity="warning",
                    title="决策后的未来公告",
                    source="test",
                    source_event_id="future-event",
                    published_at=current + timedelta(hours=1),
                ),
            ]
        )
        db.commit()
        config = db.get(StrategyConfig, config_id)

        decision = build_signal_decision(db, config, current=current)
        scores = list(
            db.scalars(
                select(QuantCandidateScore).where(
                    QuantCandidateScore.decision_id == decision.id,
                    QuantCandidateScore.stock_id.in_([stock.id for stock in stocks]),
                )
            )
        )

        assert all("命中风险公告" not in item.rejection_reasons for item in scores)


def test_signal_blocks_recent_risk_announcement(tmp_path: Path):
    engine, config_id = setup_database(tmp_path)
    as_of = date(2026, 7, 23)
    current = datetime(2026, 7, 23, 16, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        stocks = seed_signal_data(db, as_of)
        db.add(
            StockEvent(
                stock_id=stocks[0].id,
                event_type="regulatory_investigation",
                severity="critical",
                title="立案调查",
                source="test",
                source_event_id="recent-event",
                published_at=current - timedelta(days=1),
            )
        )
        db.commit()
        config = db.get(StrategyConfig, config_id)

        decision = build_signal_decision(db, config, current=current)
        rejected = db.scalar(
            select(QuantCandidateScore).where(
                QuantCandidateScore.decision_id == decision.id,
                QuantCandidateScore.stock_id == stocks[0].id,
            )
        )

        assert "命中风险公告" in rejected.rejection_reasons


def test_stock_universe_prefilter_uses_constant_query_count(tmp_path: Path):
    engine, config_id = setup_database(tmp_path)
    as_of = date(2026, 7, 23)
    current = datetime(2026, 7, 23, 16, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        stocks = seed_signal_data(db, as_of)
        db.add(
            StockEvent(
                stock_id=stocks[0].id,
                event_type="regulatory_investigation",
                severity="critical",
                title="立案调查",
                source="test",
                source_event_id="bulk-universe-event",
                published_at=current - timedelta(days=1),
            )
        )
        db.commit()
        config = db.get(StrategyConfig, config_id)
        statements = []

        def capture_select(_conn, _cursor, statement, *_args):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture_select)
        try:
            selected, blocked = _universe(
                db,
                config,
                "multi_factor_core",
                as_of,
                decision_at=current,
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture_select)

        assert len(statements) <= 3
        assert stocks[0].symbol in blocked
        assert "命中风险公告" in blocked[stocks[0].symbol]
        assert stocks[1].symbol in {stock.symbol for stock in selected}


def test_signal_decision_is_deterministic_and_ignores_future_data(tmp_path: Path):
    engine, config_id = setup_database(tmp_path)
    as_of = date(2026, 7, 23)
    current = datetime(2026, 7, 23, 16, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        stocks = seed_signal_data(db, as_of)
        config = db.get(StrategyConfig, config_id)
        first = build_signal_decision(db, config, current=current)
        first_hash = first.snapshot_sha256
        first_weights = dict(first.target_weights)

        db.add(
            MarketDailyBar(
                stock_id=stocks[0].id,
                trade_date=(as_of + timedelta(days=1)).isoformat(),
                open=999,
                high=999,
                low=999,
                close=999,
                adjusted_close=999,
                adjustment_factor=1,
                volume=99_000_000,
                amount=9_900_000_000,
                source="future-row",
            )
        )
        db.commit()
        second = build_signal_decision(db, config, current=current)

        assert second.id == first.id
        assert second.snapshot_sha256 == first_hash
        assert second.target_weights == first_weights
        assert second.snapshot_payload["strategy_key"] == "multi_factor_core"
        assert second.snapshot_payload["as_of"] == as_of.isoformat()
        assert second.snapshot_payload["inputs"]["000001.SZ"]["bar_count"] == 270
        assert second.snapshot_payload["inputs"]["000001.SZ"]["sources"] == [
            "test-real"
        ]
        assert sum(second.target_weights.values()) <= 0.80
        scores = list(
            db.scalars(
                select(QuantCandidateScore).where(
                    QuantCandidateScore.decision_id == first.id
                )
            )
        )
        selected = [item for item in scores if item.status == "selected"]
        rejected = [item for item in scores if item.status == "rejected"]
        assert len(selected) == 2
        assert rejected
        assert all(item.rejection_reasons for item in rejected)


def test_signal_fails_closed_when_required_point_in_time_data_is_missing(tmp_path: Path):
    engine, config_id = setup_database(tmp_path)
    current = datetime(2026, 7, 23, 16, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_id)

        with pytest.raises(DataNotReadyError, match="没有满足数据要求"):
            build_signal_decision(db, config, current=current)


def test_signal_rejects_demo_and_unadjusted_stock_bars(tmp_path: Path):
    engine, config_id = setup_database(tmp_path)
    as_of = date(2026, 7, 23)
    current = datetime(2026, 7, 23, 16, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        stocks = seed_signal_data(db, as_of)
        config = db.get(StrategyConfig, config_id)
        rows = list(
            db.scalars(
                select(MarketDailyBar).where(
                    MarketDailyBar.stock_id.in_([stock.id for stock in stocks])
                )
            )
        )
        for row in rows:
            row.adjusted_close = None
            row.adjustment_factor = None
        rows[-1].source = "demo"
        db.commit()

        with pytest.raises(DataNotReadyError, match="没有满足数据要求"):
            build_signal_decision(db, config, current=current)


def test_signal_rejects_stale_daily_history(tmp_path: Path):
    engine, config_id = setup_database(tmp_path)
    as_of = date(2026, 7, 23)
    current = datetime(2026, 7, 23, 16, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        stocks = seed_signal_data(db, as_of)
        config = db.get(StrategyConfig, config_id)
        for row in db.scalars(
            select(MarketDailyBar).where(
                MarketDailyBar.stock_id.in_([stock.id for stock in stocks]),
                MarketDailyBar.trade_date == as_of.isoformat(),
            )
        ):
            db.delete(row)
        db.commit()

        with pytest.raises(DataNotReadyError, match="没有满足数据要求"):
            build_signal_decision(db, config, current=current)


def test_strategy_filters_can_produce_valid_empty_decision(tmp_path: Path):
    engine, _ = setup_database(tmp_path)
    as_of = date(2026, 7, 23)
    current = datetime(2026, 7, 23, 16, 32, tzinfo=SHANGHAI)
    with Session(engine) as db:
        seed_signal_data(db, as_of)
        for stock in db.scalars(
            select(Stock).where(Stock.instrument_type == "STOCK")
        ):
            if stock.listing_date is None:
                stock.status = "inactive"
        db.commit()
        config = db.scalar(
            select(StrategyConfig)
            .where(StrategyConfig.name == "突破趋势")
            .limit(1)
        )

        decision = build_signal_decision(db, config, current=current)

        assert decision.status == "ready"
        assert decision.target_weights == {}


def test_financial_report_available_after_as_of_is_rejected(tmp_path: Path):
    engine, config_id = setup_database(tmp_path)
    as_of = date(2026, 7, 23)
    current = datetime(2026, 7, 23, 16, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        stocks = seed_signal_data(db, as_of)
        report = db.scalar(
            select(FinancialReportSnapshot).where(
                FinancialReportSnapshot.stock_id == stocks[0].id
            )
        )
        report.available_on = (as_of + timedelta(days=1)).isoformat()
        db.commit()
        config = db.get(StrategyConfig, config_id)

        decision = build_signal_decision(db, config, current=current)
        score = db.scalar(
            select(QuantCandidateScore).where(
                QuantCandidateScore.decision_id == decision.id,
                QuantCandidateScore.stock_id == stocks[0].id,
            )
        )

        assert score.status == "rejected"
        assert "缺少点时财务数据" in score.rejection_reasons


def test_financial_rows_keep_only_latest_visible_revision_per_report_period(
    tmp_path: Path,
):
    engine, _config_id = setup_database(tmp_path)
    as_of = date(2026, 7, 23)
    with Session(engine) as db:
        stocks = seed_signal_data(db, as_of)
        db.add(
            FinancialReportSnapshot(
                stock_id=stocks[0].id,
                report_period=(as_of - timedelta(days=90)).isoformat(),
                announcement_date=(as_of - timedelta(days=5)).isoformat(),
                actual_announcement_date=(as_of - timedelta(days=5)).isoformat(),
                available_on=(as_of - timedelta(days=4)).isoformat(),
                eps=9.9,
                roe=0.20,
                gross_margin=0.40,
                operating_cash_flow=30,
                total_assets=100,
                total_liabilities=20,
                source="test-real",
            )
        )
        db.commit()

        rows = _financial_rows(db, stocks[0].id, as_of)

        matching = [
            row
            for row in rows
            if row.report_period == as_of - timedelta(days=90)
        ]
        assert len(matching) == 1
        assert matching[0].eps == pytest.approx(9.9)


def test_dry_run_decision_does_not_consume_same_day_signal(tmp_path: Path):
    engine, config_id = setup_database(tmp_path)
    as_of = date(2026, 7, 23)
    current = datetime(2026, 7, 23, 16, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        seed_signal_data(db, as_of)
        config = db.get(StrategyConfig, config_id)

        dry_run = build_signal_decision(
            db,
            config,
            current=current,
            decision_type="dry_run",
        )
        signal = build_signal_decision(
            db,
            config,
            current=current,
            decision_type="signal",
        )

        assert dry_run.id != signal.id
        assert dry_run.decision_type == "dry_run"
        assert signal.decision_type == "signal"
        assert dry_run.snapshot_sha256 == signal.snapshot_sha256


def test_same_day_configuration_change_creates_new_dry_run_decision(
    tmp_path: Path,
):
    engine, config_id = setup_database(tmp_path)
    as_of = date(2026, 7, 23)
    current = datetime(2026, 7, 23, 16, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        seed_signal_data(db, as_of)
        config = db.get(StrategyConfig, config_id)
        first = build_signal_decision(
            db,
            config,
            current=current,
            decision_type="dry_run",
        )
        config.parameters = {**config.parameters, "prefilter_size": 500}
        db.commit()

        second = build_signal_decision(
            db,
            config,
            current=current,
            decision_type="dry_run",
        )

        assert second.id != first.id
        assert second.config_fingerprint != first.config_fingerprint


def test_signal_passes_existing_account_holdings_to_policy(
    tmp_path: Path,
    monkeypatch,
):
    engine, config_id = setup_database(tmp_path)
    as_of = date(2026, 7, 23)
    current = datetime(2026, 7, 23, 16, 30, tzinfo=SHANGHAI)
    captured = {}
    with Session(engine) as db:
        stocks = seed_signal_data(db, as_of)
        config = db.get(StrategyConfig, config_id)
        account = db.get(SimulationAccount, config.simulation_account_id)
        db.add(
            Position(
                account_id=account.id,
                mode="SIMULATION",
                stock_id=stocks[0].id,
                quantity=1000,
                available_quantity=1000,
                average_cost=10,
                market_value=0,
            )
        )
        db.commit()

        from app.quant_strategies import signals

        original = signals.apply_holding_policy

        def capture(result, **kwargs):
            captured["holdings"] = list(kwargs["holdings"])
            return original(result, **kwargs)

        monkeypatch.setattr(signals, "apply_holding_policy", capture)

        build_signal_decision(db, config, current=current)

        assert len(captured["holdings"]) == 1
        assert captured["holdings"][0].symbol == stocks[0].symbol
        assert captured["holdings"][0].latest_close > 0
        assert captured["holdings"][0].current_weight > 0
