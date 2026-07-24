from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import (
    DataSourceState,
    Order,
    ProbabilityPortfolioRun,
    Stock,
    StrategyConfig,
    StrategyDefinition,
    StrategyRun,
    StrategySchedule,
    now,
)
from app.services import seed_database


def test_scheduler_executes_due_simulation_schedule_exactly_once(
    tmp_path: Path,
    monkeypatch,
):
    from app import scheduler_runner

    engine = create_engine(f"sqlite:///{tmp_path / 'scheduler.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_database(
            db,
            Settings(database_url=f"sqlite:///{tmp_path / 'scheduler.db'}"),
        )
        definition = db.scalar(
            select(StrategyDefinition).where(
                StrategyDefinition.key == "overnight_hold"
            )
        )
        for stock in db.scalars(select(Stock)):
            stock.last_price = 10.01
            stock.change_pct = 4.9 if stock.symbol == "000001.SZ" else 0.2
            stock.turnover_amount = (
                900_000_000 if stock.symbol == "000001.SZ" else 10_000_000
            )
            stock.quote_updated_at = now()
        source = db.scalar(
            select(DataSourceState).where(
                DataSourceState.provider == "akshare_events"
            )
        )
        source.healthy = True
        source.last_checked_at = now()
        config = StrategyConfig(
            strategy_definition_id=definition.id,
            name="自动调度端到端",
            mode="SIMULATION",
            parameters={},
        )
        db.add(config)
        db.flush()
        db.add(
            StrategySchedule(
                strategy_config_id=config.id,
                trigger_type="entry_evaluation",
                run_time="14:40",
                enabled=True,
            )
        )
        db.commit()

    monkeypatch.setattr(
        scheduler_runner,
        "SessionLocal",
        lambda: Session(engine),
    )
    monkeypatch.setattr(
        scheduler_runner,
        "trading_calendar_service",
        lambda: SimpleNamespace(is_trading_day=lambda *args, **kwargs: True),
    )
    current = datetime(2026, 7, 13, 14, 40, 10, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert scheduler_runner.run_due_schedules(current=current) == 1
    assert scheduler_runner.run_due_schedules(current=current) == 0

    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 1
        schedule = db.scalar(select(StrategySchedule))
        assert schedule.last_run_id is not None
        assert schedule.last_scheduled_for == (
            "2026-07-13:entry_evaluation:14:40"
        )


def test_scheduler_releases_window_after_unexpected_strategy_error(
    tmp_path: Path,
    monkeypatch,
):
    from app import scheduler_runner

    engine = create_engine(f"sqlite:///{tmp_path / 'scheduler-error.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_database(
            db,
            Settings(database_url=f"sqlite:///{tmp_path / 'scheduler-error.db'}"),
        )
        definition = db.scalar(
            select(StrategyDefinition).where(
                StrategyDefinition.key == "overnight_hold"
            )
        )
        config = StrategyConfig(
            strategy_definition_id=definition.id,
            name="自动调度异常恢复",
            mode="SIMULATION",
            parameters={},
        )
        db.add(config)
        db.flush()
        db.add(
            StrategySchedule(
                strategy_config_id=config.id,
                trigger_type="entry_evaluation",
                run_time="14:40",
                enabled=True,
            )
        )
        db.commit()

    monkeypatch.setattr(scheduler_runner, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(
        scheduler_runner,
        "trading_calendar_service",
        lambda: SimpleNamespace(is_trading_day=lambda *args, **kwargs: True),
    )
    monkeypatch.setattr(
        scheduler_runner,
        "execute_simulation_strategy",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("temporary failure")),
    )
    current = datetime(2026, 7, 13, 14, 40, 10, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert scheduler_runner.run_due_schedules(current=current) == 0

    with Session(engine) as db:
        schedule = db.scalar(select(StrategySchedule))
        assert schedule.last_scheduled_for is None
        assert schedule.next_run_at == current.replace(tzinfo=None) + scheduler_runner.RETRY_DELAY


def test_schedule_window_claim_is_atomic(tmp_path: Path):
    from app.scheduler_runner import claim_schedule_window

    engine = create_engine(f"sqlite:///{tmp_path / 'claim.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{tmp_path / 'claim.db'}"))
        definition = db.scalar(
            select(StrategyDefinition).where(
                StrategyDefinition.key == "overnight_hold"
            )
        )
        config = StrategyConfig(
            strategy_definition_id=definition.id,
            name="原子占用测试",
            mode="SIMULATION",
            parameters={},
        )
        db.add(config)
        db.flush()
        schedule = StrategySchedule(
            strategy_config_id=config.id,
            trigger_type="entry_evaluation",
            run_time="14:40",
            enabled=True,
        )
        db.add(schedule)
        db.commit()
        schedule_id = schedule.id

    current = datetime(2026, 7, 13, 14, 40, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    window_key = "2026-07-13:entry_evaluation:14:40"
    first = Session(engine)
    second = Session(engine)
    try:
        assert claim_schedule_window(
            first,
            schedule_id=schedule_id,
            window_key=window_key,
            current=current,
        )
        assert not claim_schedule_window(
            second,
            schedule_id=schedule_id,
            window_key=window_key,
            current=current,
        )
    finally:
        first.close()
        second.close()


def test_probability_exit_schedule_retries_until_1045():
    from app.scheduler_runner import schedule_tolerance_seconds

    schedule = SimpleNamespace(trigger_type="portfolio_exit", run_time="10:30")
    config = SimpleNamespace(parameters={"latest_exit_time": "10:45"})

    assert schedule_tolerance_seconds(schedule, config) == 15 * 60


def test_probability_entry_schedule_retries_until_1441():
    from app.scheduler_runner import schedule_tolerance_seconds

    schedule = SimpleNamespace(trigger_type="portfolio_entry", run_time="14:40")
    config = SimpleNamespace(parameters={"latest_entry_time": "14:41"})

    assert schedule_tolerance_seconds(schedule, config) == 60


def test_quant_signal_schedule_can_recover_until_signal_task_deadline():
    from app.scheduler_runner import schedule_tolerance_seconds

    schedule = SimpleNamespace(trigger_type="quant_signal", run_time="16:30")
    config = SimpleNamespace(parameters={})

    assert schedule_tolerance_seconds(schedule, config) == 6 * 60 * 60 + 30 * 60


def test_quant_execution_schedule_can_recover_until_1000():
    from app.scheduler_runner import schedule_tolerance_seconds

    schedule = SimpleNamespace(trigger_type="quant_execute", run_time="09:35")
    config = SimpleNamespace(parameters={})

    assert schedule_tolerance_seconds(schedule, config) == 25 * 60


def test_probability_entry_recovery_ignores_same_window_observation(tmp_path: Path):
    from app.probability_portfolio.runtime import seed_probability_portfolio_runtime
    from app.scheduler_runner import existing_window_run

    database_url = f"sqlite:///{tmp_path / 'probability-recovery.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    current = datetime(2026, 7, 13, 14, 40, 10, tzinfo=ZoneInfo("Asia/Shanghai"))

    with Session(engine) as db:
        settings = Settings(database_url=database_url)
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        schedule = db.scalar(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id,
                StrategySchedule.trigger_type == "portfolio_entry",
            )
        )
        observation = StrategyRun(
            strategy_config_id=config.id,
            mode="SIMULATION",
            status="completed",
            started_at=current,
            finished_at=current,
            summary={},
        )
        db.add(observation)
        db.flush()
        db.add(
            ProbabilityPortfolioRun(
                strategy_run_id=observation.id,
                strategy_config_id=config.id,
                simulation_account_id=config.simulation_account_id,
                trading_date=current.date().isoformat(),
                trigger_type="portfolio_observation",
                status="completed",
                dry_run=True,
                completed_at=current,
            )
        )
        schedule.last_run_id = observation.id
        db.commit()

        assert existing_window_run(db, schedule=schedule, current=current) is None

        entry = StrategyRun(
            strategy_config_id=config.id,
            mode="SIMULATION",
            status="completed",
            started_at=current,
            finished_at=current,
            summary={},
        )
        db.add(entry)
        db.flush()
        db.add(
            ProbabilityPortfolioRun(
                strategy_run_id=entry.id,
                strategy_config_id=config.id,
                simulation_account_id=config.simulation_account_id,
                trading_date=current.date().isoformat(),
                trigger_type="portfolio_entry",
                status="completed",
                dry_run=False,
                completed_at=current,
            )
        )
        db.commit()

        recovered = existing_window_run(db, schedule=schedule, current=current)
        assert recovered is not None
        assert recovered.id == entry.id


def test_quant_schedule_recovery_ignores_unrelated_manual_run(tmp_path: Path):
    from app.quant_strategies.runtime import seed_quant_strategy_runtimes
    from app.scheduler_runner import existing_window_run

    database_url = f"sqlite:///{tmp_path / 'quant-recovery.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    current = datetime(2026, 7, 31, 16, 30, 10, tzinfo=ZoneInfo("Asia/Shanghai"))

    with Session(engine) as db:
        settings = Settings(database_url=database_url)
        seed_database(db, settings)
        config = seed_quant_strategy_runtimes(db, settings)["multi_factor_core"]
        schedule = db.scalar(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id,
                StrategySchedule.trigger_type == "quant_signal",
            )
        )
        manual = StrategyRun(
            strategy_config_id=config.id,
            mode="SIMULATION",
            status="completed",
            started_at=current,
            finished_at=current,
            summary={"dry_run": True},
        )
        db.add(manual)
        db.commit()

        assert existing_window_run(db, schedule=schedule, current=current) is None

        queued = StrategyRun(
            strategy_config_id=config.id,
            mode="SIMULATION",
            status="completed",
            started_at=current,
            finished_at=current,
            summary={"queued": True, "task_type": "signal", "task_id": 7},
        )
        db.add(queued)
        db.commit()

        recovered = existing_window_run(db, schedule=schedule, current=current)
        assert recovered is not None
        assert recovered.id == queued.id


def test_expired_claim_reconciles_completed_run_without_duplicate_order(
    tmp_path: Path,
    monkeypatch,
):
    from app import scheduler_runner

    engine = create_engine(f"sqlite:///{tmp_path / 'claim-recovery.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_database(
            db,
            Settings(database_url=f"sqlite:///{tmp_path / 'claim-recovery.db'}"),
        )
        definition = db.scalar(
            select(StrategyDefinition).where(
                StrategyDefinition.key == "overnight_hold"
            )
        )
        for stock in db.scalars(select(Stock)):
            stock.last_price = 10.01
            stock.change_pct = 4.9 if stock.symbol == "000001.SZ" else 0.2
            stock.turnover_amount = (
                900_000_000 if stock.symbol == "000001.SZ" else 10_000_000
            )
            stock.quote_updated_at = now()
        source = db.scalar(
            select(DataSourceState).where(
                DataSourceState.provider == "akshare_events"
            )
        )
        source.healthy = True
        source.last_checked_at = now()
        config = StrategyConfig(
            strategy_definition_id=definition.id,
            name="崩溃恢复测试",
            mode="SIMULATION",
            parameters={},
        )
        db.add(config)
        db.flush()
        db.add(
            StrategySchedule(
                strategy_config_id=config.id,
                trigger_type="entry_evaluation",
                run_time="14:40",
                enabled=True,
            )
        )
        db.commit()

    monkeypatch.setattr(scheduler_runner, "SessionLocal", lambda: Session(engine))
    monkeypatch.setattr(
        scheduler_runner,
        "trading_calendar_service",
        lambda: SimpleNamespace(is_trading_day=lambda *args, **kwargs: True),
    )
    current = datetime(2026, 7, 13, 14, 40, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert scheduler_runner.run_due_schedules(current=current) == 1

    with Session(engine) as db:
        schedule = db.scalar(select(StrategySchedule))
        run = db.get(StrategyRun, schedule.last_run_id)
        run.started_at = current
        schedule.next_run_at = current.replace(tzinfo=None)
        db.commit()

    def duplicate_execution(*args, **kwargs):
        raise AssertionError("completed claim must not execute again")

    monkeypatch.setattr(
        scheduler_runner,
        "execute_simulation_strategy",
        duplicate_execution,
    )

    assert scheduler_runner.run_due_schedules(current=current) == 0
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 1
        schedule = db.scalar(select(StrategySchedule))
        assert schedule.next_run_at is None
