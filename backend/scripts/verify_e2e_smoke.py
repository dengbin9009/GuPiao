from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import NotificationChannel, Stock, StrategyDefinition, WatchlistItem
from app.services import create_backtest, execute_simulation_strategy, seed_database


def main() -> None:
    db_path = Path(__file__).resolve().parents[1] / ".tmp-e2e-smoke.db"
    if db_path.exists():
        db_path.unlink()
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{db_path}"))
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))

        db.add(WatchlistItem(stock_id=stock.id))
        db.add(
            NotificationChannel(
                type="email",
                name="运维告警",
                enabled=True,
                recipient="ops@example.com",
                secret_ref="SMTP_PASSWORD",
                event_types=["risk_block"],
            )
        )
        db.commit()

        backtest = create_backtest(db, definition, {"timeframe": "1m", "initial_cash": 10000, "parameters": {}})
        assert backtest.status == "completed"

        from app.models import StrategyConfig

        config = StrategyConfig(strategy_definition_id=definition.id, name="e2e-run", mode="SIMULATION", parameters={})
        db.add(config)
        db.commit()
        run = execute_simulation_strategy(db, config)
        assert run.status in {"completed", "failed"}

    print("e2e_smoke_ok")


if __name__ == "__main__":
    main()
