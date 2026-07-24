from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import (
    DataSourceState,
    Position,
    QuantPortfolioDecision,
    SimulationAccount,
    Stock,
    StockEvent,
    StrategyConfig,
    StrategyDefinition,
    StrategySchedule,
    WatchlistItem,
)
from app.services import execute_simulation_strategy, seed_database


def make_db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'poll.db'}")
    Base.metadata.create_all(engine)
    return engine


def make_target_top_ranked(db: Session) -> Stock:
    target = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    for stock in db.scalars(select(Stock)).all():
        if stock.symbol == target.symbol:
            stock.last_price = 10.01
            stock.change_pct = 4.9
            stock.turnover_amount = 900_000_000
        else:
            stock.change_pct = 0.2
            stock.turnover_amount = 10_000_000
    db.commit()
    return target


def test_worker_poll_updates_quote_timestamp(tmp_path: Path):
    engine = make_db(tmp_path)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{tmp_path / 'poll.db'}"))
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        db.add(WatchlistItem(stock_id=stock.id))
        db.commit()

        from app.worker import poll_watchlist_quotes

        class QuoteProvider:
            name = "akshare"

            def quotes(self, symbols):
                return [{"代码": "000001", "最新价": 13.01, "涨跌幅": 0.88, "成交额": 99999999}]

        result = poll_watchlist_quotes(provider=QuoteProvider(), session_factory=lambda: Session(engine))

        db.refresh(stock)
        source = db.scalar(select(DataSourceState).where(DataSourceState.provider == "akshare"))
        assert result == {"updated": 1, "missing": 0, "errors": 0}
        assert stock.last_price == 13.01
        assert stock.quote_updated_at is not None
        assert source.last_quote_at is not None


def test_worker_strategy_poll_refreshes_full_active_universe(tmp_path: Path):
    engine = make_db(tmp_path)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{tmp_path / 'poll.db'}"))
        expected = list(
            db.scalars(
                select(Stock.symbol).where(
                    Stock.status == "active",
                    Stock.exchange.in_(["SSE", "SZSE"]),
                )
            )
        )

    requested = []

    class QuoteProvider:
        name = "mootdx"

        def quotes(self, symbols):
            requested.extend(symbols)
            return [
                {
                    "代码": symbol.split(".")[0],
                    "最新价": 10.01,
                    "涨跌幅": 2.0,
                    "成交额": 200_000_000,
                }
                for symbol in symbols
            ]

    from app.worker import poll_strategy_quotes

    result = poll_strategy_quotes(
        provider=QuoteProvider(),
        session_factory=lambda: Session(engine),
    )

    assert requested == expected
    assert result == {"updated": len(expected), "missing": 0, "errors": 0}


def test_worker_quote_router_falls_back_to_mootdx(tmp_path: Path, monkeypatch):
    from app.market_data import MarketDataError, ProviderRouter
    from app.worker import poll_strategy_quotes

    engine = make_db(tmp_path)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{tmp_path / 'poll.db'}"))

    class BrokenProvider:
        name = "akshare"
        capabilities = frozenset({"realtime"})

        def health(self):
            return True, None

        def quotes(self, symbols):
            raise MarketDataError("upstream disconnected")

    class WorkingProvider:
        name = "mootdx"
        capabilities = frozenset({"realtime"})

        def health(self):
            return True, None

        def quotes(self, symbols):
            return [
                {
                    "代码": symbol.split(".")[0],
                    "最新价": 10.01,
                    "涨跌幅": 2.0,
                    "成交额": 200_000_000,
                }
                for symbol in symbols
            ]

    router = ProviderRouter([BrokenProvider(), WorkingProvider()])
    monkeypatch.setattr("app.worker.market_router", lambda: router)

    result = poll_strategy_quotes(session_factory=lambda: Session(engine))

    assert result["updated"] == 5
    with Session(engine) as db:
        source = db.scalar(
            select(DataSourceState).where(DataSourceState.provider == "mootdx")
        )
        assert source is not None and source.healthy


def test_worker_corporate_event_poll_marks_source_healthy(tmp_path: Path):
    from app.worker import poll_corporate_events

    engine = make_db(tmp_path)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{tmp_path / 'poll.db'}"))

    class EventProvider:
        name = "akshare_events"

        def events(self, *, symbols, start, end):
            assert "000001.SZ" in symbols
            return [
                {
                    "source": "akshare",
                    "source_event_id": "real-1",
                    "symbol": "000001.SZ",
                    "title": "董事会决议公告",
                    "event_type": "announcement",
                }
            ]

    result = poll_corporate_events(
        provider=EventProvider(),
        session_factory=lambda: Session(engine),
    )

    assert result == {"created": 1, "updated": 0, "errors": 0}
    with Session(engine) as db:
        source = db.scalar(
            select(DataSourceState).where(
                DataSourceState.provider == "akshare_events"
            )
        )
        event = db.scalar(select(StockEvent).where(StockEvent.source_event_id == "real-1"))
        assert source is not None and source.healthy and source.last_checked_at is not None
        assert event is not None


def test_worker_quote_polling_is_limited_to_execution_windows():
    from app.worker import quote_poll_scope

    shanghai = ZoneInfo("Asia/Shanghai")

    assert quote_poll_scope(datetime(2026, 7, 13, 14, 39, 40, tzinfo=shanghai)) == "entry"
    assert quote_poll_scope(datetime(2026, 7, 14, 9, 34, 40, tzinfo=shanghai)) == "exit"
    assert quote_poll_scope(datetime(2026, 7, 14, 9, 59, 40, tzinfo=shanghai)) == "exit"
    assert quote_poll_scope(datetime(2026, 7, 14, 10, 30, 0, tzinfo=shanghai)) == "exit"
    assert quote_poll_scope(datetime(2026, 7, 14, 10, 45, 0, tzinfo=shanghai)) == "exit"
    assert quote_poll_scope(datetime(2026, 7, 14, 10, 45, 1, tzinfo=shanghai)) is None
    assert quote_poll_scope(datetime(2026, 7, 10, 20, 30, tzinfo=shanghai)) is None
    assert quote_poll_scope(datetime(2026, 7, 11, 14, 39, 40, tzinfo=shanghai)) is None


def test_worker_agent_snapshot_scope_runs_once_in_pre_analysis_window():
    from app.worker import agent_snapshot_scope

    shanghai = ZoneInfo("Asia/Shanghai")
    assert agent_snapshot_scope(datetime(2026, 7, 13, 13, 25, tzinfo=shanghai))
    assert agent_snapshot_scope(datetime(2026, 7, 13, 13, 29, 59, tzinfo=shanghai))
    assert not agent_snapshot_scope(datetime(2026, 7, 13, 13, 30, tzinfo=shanghai))
    assert not agent_snapshot_scope(datetime(2026, 7, 11, 13, 25, tzinfo=shanghai))


def test_worker_probability_snapshot_scope_is_limited_to_final_pre_entry_minute():
    from app.worker import (
        probability_observation_scope,
        probability_preheat_scope,
        probability_snapshot_scope,
    )

    shanghai = ZoneInfo("Asia/Shanghai")
    assert not probability_snapshot_scope(
        datetime(2026, 7, 13, 14, 39, 59, tzinfo=shanghai)
    )
    assert probability_snapshot_scope(
        datetime(2026, 7, 13, 14, 40, 0, tzinfo=shanghai)
    )
    assert probability_snapshot_scope(
        datetime(2026, 7, 13, 14, 40, 29, tzinfo=shanghai)
    )
    assert not probability_snapshot_scope(
        datetime(2026, 7, 13, 14, 38, 59, tzinfo=shanghai)
    )
    assert not probability_snapshot_scope(
        datetime(2026, 7, 13, 14, 40, 30, tzinfo=shanghai)
    )
    assert probability_observation_scope(
        datetime(2026, 7, 13, 14, 40, 0, tzinfo=shanghai)
    )
    assert probability_observation_scope(
        datetime(2026, 7, 13, 14, 40, 59, tzinfo=shanghai)
    )
    assert not probability_observation_scope(
        datetime(2026, 7, 13, 14, 41, 0, tzinfo=shanghai)
    )
    assert probability_preheat_scope(
        datetime(2026, 7, 13, 13, 40, 0, tzinfo=shanghai)
    )
    assert probability_preheat_scope(
        datetime(2026, 7, 13, 14, 9, 59, tzinfo=shanghai)
    )
    assert not probability_preheat_scope(
        datetime(2026, 7, 13, 14, 10, 0, tzinfo=shanghai)
    )
    assert not probability_preheat_scope(
        datetime(2026, 7, 11, 13, 40, 0, tzinfo=shanghai)
    )


def test_worker_probability_label_scope_is_1030_to_1045():
    from app.worker import probability_label_scope

    shanghai = ZoneInfo("Asia/Shanghai")
    assert probability_label_scope(
        datetime(2026, 7, 14, 10, 30, 0, tzinfo=shanghai)
    )
    assert probability_label_scope(
        datetime(2026, 7, 14, 10, 45, 0, tzinfo=shanghai)
    )
    assert not probability_label_scope(
        datetime(2026, 7, 14, 10, 45, 1, tzinfo=shanghai)
    )


def test_worker_probability_observation_delegates_without_orders(
    tmp_path: Path,
    monkeypatch,
):
    from app.probability_portfolio.candidates import CandidateBuildResult
    from app.probability_portfolio.runtime import seed_probability_portfolio_runtime
    from app.worker import poll_probability_observation

    engine = make_db(tmp_path)
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'poll.db'}")
    with Session(engine) as db:
        seed_database(db, settings)
        seed_probability_portfolio_runtime(db, settings)

    captured = []
    monkeypatch.setattr(
        "app.worker.build_scored_candidates",
        lambda *args, **kwargs: CandidateBuildResult([], [], ("模型未就绪",), None),
    )

    def fake_record(db, config, **kwargs):
        captured.append((config.id, kwargs))
        return type("Run", (), {"summary": {"accepted": 0}})()

    monkeypatch.setattr("app.worker.record_probability_observation", fake_record)
    current = datetime(2026, 7, 13, 14, 40, 5, tzinfo=ZoneInfo("Asia/Shanghai"))

    result = poll_probability_observation(
        session_factory=lambda: Session(engine),
        current=current,
    )

    assert result == {"accepted": 0}
    assert captured[0][1]["current"] == current


def test_probability_fallback_observation_runs_with_fresh_in_window_time(
    monkeypatch,
):
    from app.worker import poll_due_probability_observation

    called = []
    monkeypatch.setattr(
        "app.worker.poll_probability_observation",
        lambda **kwargs: called.append(kwargs) or {"accepted": 0},
    )
    current = datetime(2026, 7, 23, 14, 40, 50, tzinfo=ZoneInfo("Asia/Shanghai"))

    observed_date, result = poll_due_probability_observation(
        last_observation_date=None,
        current=current,
    )

    assert observed_date == current.date()
    assert result == {"accepted": 0}
    assert called == [{"current": current}]


def test_probability_fallback_observation_skips_after_1441(monkeypatch):
    from app.worker import poll_due_probability_observation

    monkeypatch.setattr(
        "app.worker.poll_probability_observation",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("14:41后不得使用快照开始前的旧时间补记观察")
        ),
    )
    current = datetime(2026, 7, 23, 14, 41, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    observed_date, result = poll_due_probability_observation(
        last_observation_date=None,
        current=current,
    )

    assert observed_date is None
    assert result is None


def test_probability_snapshot_completion_records_observation_after_1440(
    tmp_path: Path,
    monkeypatch,
):
    from app.probability_portfolio.candidates import CandidateBuildResult
    from app.probability_portfolio.runtime import seed_probability_portfolio_runtime
    from app.worker import poll_probability_market_snapshot

    engine = make_db(tmp_path)
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'poll.db'}")
    with Session(engine) as db:
        seed_database(db, settings)
        seed_probability_portfolio_runtime(db, settings)

    captured = []
    candidate_times = []
    monkeypatch.setattr(
        "app.worker.sync_probability_market_data",
        lambda *args, **kwargs: {"quote_updated": 5, "errors": 0},
    )
    monkeypatch.setattr(
        "app.worker.build_scored_candidates",
        lambda *args, **kwargs: (
            candidate_times.append(kwargs["current"])
            or CandidateBuildResult([], [], ("概率模型尚未就绪",), None)
        ),
    )

    def fake_record(db, config, **kwargs):
        captured.append(kwargs)
        return type("Run", (), {"id": 19, "summary": {"accepted": 0}})()

    monkeypatch.setattr("app.worker.record_probability_observation", fake_record)
    started = datetime(2026, 7, 23, 14, 40, 7, tzinfo=ZoneInfo("Asia/Shanghai"))
    completed = datetime(2026, 7, 23, 14, 40, 20, tzinfo=ZoneInfo("Asia/Shanghai"))

    result = poll_probability_market_snapshot(
        router=object(),
        session_factory=lambda: Session(engine),
        current=started,
        completed_at=completed,
    )

    assert result["observation_run_id"] == 19
    assert captured[0]["current"].hour == 14
    assert captured[0]["current"].minute == 40
    assert captured[0]["current"] == completed
    assert candidate_times == [completed]


def test_probability_snapshot_completion_after_1441_does_not_record_observation(
    tmp_path: Path,
    monkeypatch,
):
    from app.probability_portfolio.runtime import seed_probability_portfolio_runtime
    from app.worker import poll_probability_market_snapshot

    engine = make_db(tmp_path)
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'poll.db'}")
    with Session(engine) as db:
        seed_database(db, settings)
        seed_probability_portfolio_runtime(db, settings)

    monkeypatch.setattr(
        "app.worker.sync_probability_market_data",
        lambda *args, **kwargs: {"quote_updated": 5, "errors": 0},
    )
    monkeypatch.setattr(
        "app.worker.record_probability_observation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("14:41后完成的快照不得创建训练观察")
        ),
    )

    result = poll_probability_market_snapshot(
        router=object(),
        session_factory=lambda: Session(engine),
        current=datetime(2026, 7, 23, 14, 40, 20, tzinfo=ZoneInfo("Asia/Shanghai")),
        completed_at=datetime(2026, 7, 23, 14, 41, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result == {"quote_updated": 5, "errors": 0}


def test_probability_preheat_never_records_observation(tmp_path: Path, monkeypatch):
    from app.probability_portfolio.runtime import seed_probability_portfolio_runtime
    from app.worker import poll_probability_market_snapshot

    engine = make_db(tmp_path)
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'poll.db'}")
    with Session(engine) as db:
        seed_database(db, settings)
        seed_probability_portfolio_runtime(db, settings)

    monkeypatch.setattr(
        "app.worker.sync_probability_market_data",
        lambda *args, **kwargs: {"quote_updated": 5, "errors": 0},
    )
    monkeypatch.setattr(
        "app.worker.record_probability_observation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("预热不得创建训练观察")
        ),
    )

    result = poll_probability_market_snapshot(
        router=object(),
        session_factory=lambda: Session(engine),
        current=datetime(2026, 7, 23, 13, 40, tzinfo=ZoneInfo("Asia/Shanghai")),
        completed_at=datetime(2026, 7, 23, 14, 41, tzinfo=ZoneInfo("Asia/Shanghai")),
        record_observation=False,
    )

    assert result == {"quote_updated": 5, "errors": 0}


def test_worker_probability_labels_refresh_only_pending_observations(
    tmp_path: Path,
    monkeypatch,
):
    from app.probability_portfolio.runtime import seed_probability_portfolio_runtime
    from app.worker import poll_probability_training_labels

    engine = make_db(tmp_path)
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'poll.db'}")
    with Session(engine) as db:
        seed_database(db, settings)
        seed_probability_portfolio_runtime(db, settings)

    requested = []
    trained = []

    class Provider:
        name = "mootdx"

        def quotes(self, symbols):
            requested.extend(symbols)
            return []

    monkeypatch.setattr(
        "app.worker.pending_observation_symbols",
        lambda *args, **kwargs: ["000001.SZ", "600519.SH"],
    )
    monkeypatch.setattr(
        "app.worker.finalize_probability_training_samples",
        lambda *args, **kwargs: {"created": 2, "skipped": 0, "errors": 0},
    )
    monkeypatch.setattr(
        "app.worker._refresh_symbol_quotes",
        lambda db, symbols, provider=None: (
            requested.extend(symbols)
            or type("Result", (), {"updated": len(symbols), "missing": []})()
        ),
    )
    monkeypatch.setattr(
        "app.worker.train_and_store_probability_model",
        lambda db, **kwargs: (
            trained.append(kwargs)
            or type(
                "Artifact",
                (),
                {
                    "id": 17,
                    "status": "rejected",
                    "training_sample_count": 2,
                    "calibration_sample_count": 0,
                },
            )()
        ),
    )

    result = poll_probability_training_labels(
        provider=Provider(),
        session_factory=lambda: Session(engine),
        current=datetime(2026, 7, 14, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert requested == ["000001.SZ", "600519.SH"]
    assert result["created"] == 2
    assert result["quote_updated"] == 2
    assert result["model_artifact_id"] == 17
    assert trained[0]["through"].isoformat() == "2026-07-14"
    with Session(engine) as db:
        schedules = list(db.scalars(select(StrategySchedule)))
        assert schedules and all(not item.enabled for item in schedules)


def test_worker_probability_labels_reports_calendar_failure_for_retry(
    tmp_path: Path,
    monkeypatch,
):
    from app.probability_portfolio.runtime import seed_probability_portfolio_runtime
    from app.worker import poll_probability_training_labels

    engine = make_db(tmp_path)
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'poll.db'}")
    with Session(engine) as db:
        seed_database(db, settings)
        seed_probability_portfolio_runtime(db, settings)

    monkeypatch.setattr(
        "app.worker.pending_observation_symbols",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("交易日历暂不可用")
        ),
    )

    result = poll_probability_training_labels(
        session_factory=lambda: Session(engine),
        current=datetime(2026, 7, 24, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result == {
        "created": 0,
        "skipped": 0,
        "errors": 1,
        "quote_updated": 0,
    }


def test_worker_throttles_failed_agent_snapshot_retries():
    from app.worker import agent_snapshot_retry_due

    assert agent_snapshot_retry_due(None, current_seconds=100)
    assert not agent_snapshot_retry_due(100, current_seconds=219)
    assert agent_snapshot_retry_due(100, current_seconds=220)


def test_worker_allows_dry_run_snapshot_before_schedules_are_enabled(
    tmp_path: Path,
    monkeypatch,
):
    from app.trading_agents.runtime import seed_trading_agents_runtime
    from app.worker import poll_agent_market_snapshot

    engine = make_db(tmp_path)
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'poll.db'}")
    with Session(engine) as db:
        seed_database(db, settings)
        config = seed_trading_agents_runtime(db, settings)
        assert config.parameters["dry_run"] is True
        schedules = list(
            db.scalars(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id == config.id
                )
            )
        )
        assert schedules and all(not schedule.enabled for schedule in schedules)

    captured = []

    def fake_sync(db, config, router, *, current):
        captured.append((config.id, router, current))
        return {"quote_updated": 5, "daily_rows": 300}

    monkeypatch.setattr("app.worker.sync_agent_market_data", fake_sync)
    current = datetime(2026, 7, 13, 13, 25, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = poll_agent_market_snapshot(
        router=object(),
        session_factory=lambda: Session(engine),
        current=current,
    )

    assert result == {"quote_updated": 5, "daily_rows": 300}
    assert captured and captured[0][2] == current


def test_worker_event_polling_runs_before_quote_preheat_window():
    from app.worker import (
        event_poll_scope,
        notification_poll_allowed,
        should_poll_events,
    )

    shanghai = ZoneInfo("Asia/Shanghai")

    assert event_poll_scope(datetime(2026, 7, 13, 14, 20, tzinfo=shanghai))
    assert event_poll_scope(datetime(2026, 7, 13, 14, 34, 59, tzinfo=shanghai))
    assert not event_poll_scope(datetime(2026, 7, 13, 14, 35, tzinfo=shanghai))
    assert not event_poll_scope(datetime(2026, 7, 11, 14, 20, tzinfo=shanghai))
    current = datetime(2026, 7, 13, 14, 25, tzinfo=shanghai)
    assert should_poll_events(
        current,
        seconds_since_attempt=300,
        retry_seconds=300,
    )
    assert not should_poll_events(
        current,
        seconds_since_attempt=299,
        retry_seconds=300,
    )
    assert not notification_poll_allowed(
        datetime(2026, 7, 13, 14, 25, tzinfo=shanghai)
    )
    assert not notification_poll_allowed(
        datetime(2026, 7, 14, 9, 40, tzinfo=shanghai)
    )
    assert notification_poll_allowed(
        datetime(2026, 7, 13, 12, 0, tzinfo=shanghai)
    )


def test_worker_exit_poll_refreshes_only_open_position_symbols(tmp_path: Path):
    from app.worker import poll_position_quotes

    engine = make_db(tmp_path)
    requested = []
    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{tmp_path / 'poll.db'}"))
        account = db.scalar(select(SimulationAccount))
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        db.add(
            Position(
                account_id=account.id,
                mode="SIMULATION",
                stock_id=stock.id,
                quantity=100,
                available_quantity=100,
                average_cost=10,
                market_value=1000,
                unrealized_pnl=0,
            )
        )
        db.commit()

    class QuoteProvider:
        name = "mootdx"

        def quotes(self, symbols):
            requested.extend(symbols)
            return [
                {
                    "代码": "000001",
                    "最新价": 10.1,
                    "涨跌幅": 1.0,
                    "成交额": 100_000_000,
                }
            ]

    result = poll_position_quotes(
        provider=QuoteProvider(),
        session_factory=lambda: Session(engine),
    )

    assert requested == ["000001.SZ"]
    assert result == {"updated": 1, "missing": 0, "errors": 0}


def test_worker_quant_execution_poll_refreshes_holdings_and_pending_targets(
    tmp_path: Path,
):
    from app.quant_strategies.readiness import configuration_fingerprint
    from app.quant_strategies.runtime import seed_quant_strategy_runtimes
    from app.worker import poll_quant_execution_quotes

    engine = make_db(tmp_path)
    requested = []
    current = datetime(2026, 7, 27, 9, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
    with Session(engine) as db:
        settings = Settings(
            database_url=f"sqlite:///{tmp_path / 'poll.db'}",
            live_enabled=False,
            broker_adapter="simulation",
        )
        seed_database(db, settings)
        configs = seed_quant_strategy_runtimes(db, settings)
        config = configs["multi_factor_core"]
        definition = db.get(StrategyDefinition, config.strategy_definition_id)
        held = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        target = db.scalar(select(Stock).where(Stock.symbol == "000858.SZ"))
        db.add(
            Position(
                account_id=config.simulation_account_id,
                mode="SIMULATION",
                stock_id=held.id,
                quantity=100,
                available_quantity=100,
                average_cost=10,
                market_value=1000,
            )
        )
        db.add(
            QuantPortfolioDecision(
                strategy_config_id=config.id,
                simulation_account_id=config.simulation_account_id,
                trading_date="2026-07-24",
                decision_type="signal",
                status="ready",
                data_as_of=current,
                config_fingerprint=configuration_fingerprint(
                    config.parameters,
                    simulation_account_id=config.simulation_account_id,
                    strategy_version=definition.version,
                ),
                strategy_version=definition.version,
                data_version="1",
                target_weights={target.symbol: 0.10},
            )
        )
        db.commit()

    class QuoteProvider:
        name = "mootdx"

        def quotes(self, symbols):
            requested.extend(symbols)
            return [
                {
                    "代码": symbol.split(".")[0],
                    "最新价": 10.1,
                    "涨跌幅": 1.0,
                    "成交额": 100_000_000,
                }
                for symbol in symbols
            ]

    result = poll_quant_execution_quotes(
        provider=QuoteProvider(),
        session_factory=lambda: Session(engine),
        current=current,
    )

    assert requested == ["000001.SZ", "000858.SZ"]
    assert result == {"updated": 2, "missing": 0, "errors": 0}


def test_worker_quote_failure_rolls_back_before_marking_provider(tmp_path: Path):
    from app.worker import poll_strategy_quotes

    engine = make_db(tmp_path)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{tmp_path / 'poll.db'}"))

    class BrokenProvider:
        name = "mootdx"

        def quotes(self, symbols):
            raise RuntimeError("database is locked")

    result = poll_strategy_quotes(
        provider=BrokenProvider(),
        session_factory=lambda: Session(engine),
    )

    assert result["errors"] == 1
    with Session(engine) as db:
        source = db.scalar(
            select(DataSourceState).where(DataSourceState.provider == "mootdx")
        )
        assert source.healthy is False
        assert "locked" in source.last_error


def test_worker_recovers_from_first_commit_failure(tmp_path: Path):
    from app.worker import poll_strategy_quotes

    engine = make_db(tmp_path)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{tmp_path / 'poll.db'}"))

    session = Session(engine)

    @event.listens_for(session, "before_commit", once=True)
    def fail_first_commit(_session):
        raise RuntimeError("database is locked")

    class QuoteProvider:
        name = "mootdx"

        def quotes(self, symbols):
            return [
                {
                    "代码": symbol.split(".")[0],
                    "最新价": 10.01,
                    "涨跌幅": 2.0,
                    "成交额": 200_000_000,
                }
                for symbol in symbols
            ]

    result = poll_strategy_quotes(
        provider=QuoteProvider(),
        session_factory=lambda: session,
    )

    assert result["errors"] == 1
    with Session(engine) as db:
        source = db.scalar(
            select(DataSourceState).where(DataSourceState.provider == "mootdx")
        )
        assert source.healthy is False
        assert "locked" in source.last_error


def test_stale_quote_blocks_simulation_order(tmp_path: Path):
    engine = make_db(tmp_path)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{tmp_path / 'poll.db'}"))
        definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
        stock = make_target_top_ranked(db)
        stock.quote_updated_at = None
        config = StrategyConfig(strategy_definition_id=definition.id, name="stale test", mode="SIMULATION", parameters={})
        db.add(config)
        db.commit()

        run = execute_simulation_strategy(db, config)

        assert run.summary["accepted"] == 0
        assert "行情时间缺失" in run.summary["reason"]


def test_fresh_quote_allows_simulation_precheck(tmp_path: Path):
    engine = make_db(tmp_path)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{tmp_path / 'poll.db'}"))
        definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
        stock = make_target_top_ranked(db)
        db.add(WatchlistItem(stock_id=stock.id))
        db.commit()

        from app.worker import poll_watchlist_quotes

        class QuoteProvider:
            name = "akshare"

            def quotes(self, symbols):
                return [{"代码": "000001", "最新价": 10.01, "涨跌幅": 4.9, "成交额": 900000000}]

        poll_watchlist_quotes(provider=QuoteProvider(), session_factory=lambda: Session(engine))
        config = StrategyConfig(strategy_definition_id=definition.id, name="fresh test", mode="SIMULATION", parameters={})
        db.add(config)
        db.commit()

        run = execute_simulation_strategy(db, config)

        assert run.summary["symbol"] == "000001.SZ"
