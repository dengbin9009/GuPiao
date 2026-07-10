from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .config import get_settings
from .database import Base, SessionLocal, engine
from .models import StrategyConfig, StrategySchedule, now
from .providers import trading_calendar_service
from .scheduler import evaluate_schedule
from .services import execute_simulation_exit, execute_simulation_strategy, seed_database


def run_due_schedules(current: datetime | None = None) -> int:
    current = current or datetime.now(ZoneInfo("Asia/Shanghai"))
    calendar = trading_calendar_service()
    executed = 0
    with SessionLocal() as db:
        schedules = list(
            db.scalars(
                select(StrategySchedule)
                .where(StrategySchedule.enabled.is_(True))
                .order_by(StrategySchedule.id)
            )
        )
        for schedule in schedules:
            decision = evaluate_schedule(
                trigger_type=schedule.trigger_type,
                run_time=schedule.run_time,
                enabled=schedule.enabled,
                last_scheduled_for=schedule.last_scheduled_for,
                current=current,
                trading_day_fn=calendar.is_trading_day,
            )
            if not decision.should_run:
                continue
            schedule.last_scheduled_for = decision.window_key
            db.commit()
            config = db.get(StrategyConfig, schedule.strategy_config_id)
            if not config or not config.enabled:
                continue
            run = (
                execute_simulation_exit(db, config)
                if schedule.trigger_type == "exit_evaluation"
                else execute_simulation_strategy(db, config)
            )
            schedule.last_run_id = run.id
            schedule.updated_at = now()
            db.commit()
            executed += 1
    return executed


def main() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_database(db, get_settings())
    while True:
        run_due_schedules()
        time.sleep(5)


if __name__ == "__main__":
    main()
