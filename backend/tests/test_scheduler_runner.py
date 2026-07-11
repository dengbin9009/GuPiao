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
