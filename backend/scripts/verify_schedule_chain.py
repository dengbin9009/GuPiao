from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import Stock, StrategyConfig, StrategyDefinition, StrategySchedule, StrategyRun, WatchlistItem
from app.scheduler import evaluate_schedule
from app.services import seed_database
from app.worker import poll_watchlist_quotes


def main() -> None:
    db_path = Path(__file__).resolve().parents[1] / ".tmp-schedule-verify.db"
    if db_path.exists():
        db_path.unlink()
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    tz = ZoneInfo("Asia/Shanghai")

    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{db_path}"))
        definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        db.add(WatchlistItem(stock_id=stock.id))
        config = StrategyConfig(strategy_definition_id=definition.id, name="schedule-check", mode="SIMULATION", parameters={})
        db.add(config)
        db.commit()
        db.refresh(config)

        decision = evaluate_schedule(
            trigger_type="entry_evaluation",
            run_time="14:40:00",
            enabled=True,
            last_scheduled_for=None,
            current=datetime(2026, 6, 22, 14, 40, 20, tzinfo=tz),
            trading_day_fn=lambda _: True,
        )
        assert decision.should_run
        duplicate = evaluate_schedule(
            trigger_type="entry_evaluation",
            run_time="14:40:00",
            enabled=True,
            last_scheduled_for=decision.window_key,
            current=datetime(2026, 6, 22, 14, 40, 20, tzinfo=tz),
            trading_day_fn=lambda _: True,
        )
        assert not duplicate.should_run
        holiday = evaluate_schedule(
            trigger_type="entry_evaluation",
            run_time="14:40:00",
            enabled=True,
            last_scheduled_for=None,
            current=datetime(2026, 6, 21, 14, 40, 20, tzinfo=tz),
            trading_day_fn=lambda _: False,
        )
        assert not holiday.should_run

        class QuoteProvider:
            name = "akshare"

            def quotes(self, symbols):
                return [{"代码": "000001", "最新价": 13.21, "涨跌幅": 0.98, "成交额": 111111111}]

        poll_watchlist_quotes(provider=QuoteProvider(), session_factory=lambda: Session(engine))

        db.add(
            StrategySchedule(
                strategy_config_id=config.id,
                trigger_type="entry_evaluation",
                run_time="14:40:00",
                enabled=True,
            )
        )
        db.commit()

        assert db.scalar(select(StrategyRun).limit(1)) is None

    print("schedule_chain_ok")


if __name__ == "__main__":
    main()
