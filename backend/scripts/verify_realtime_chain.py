from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import DataSourceState, Stock, StrategyConfig, StrategyDefinition, WatchlistItem
from app.services import execute_simulation_strategy, seed_database
from app.worker import poll_watchlist_quotes


def main() -> None:
    db_path = Path(__file__).resolve().parents[1] / ".tmp-realtime-verify.db"
    if db_path.exists():
        db_path.unlink()
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{db_path}"))
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
        db.add(WatchlistItem(stock_id=stock.id))
        db.commit()

        class QuoteProvider:
            name = "akshare"

            def quotes(self, symbols):
                return [{"代码": "000001", "最新价": 13.01, "涨跌幅": 0.88, "成交额": 99999999}]

        result = poll_watchlist_quotes(provider=QuoteProvider(), session_factory=lambda: Session(engine))
        db.refresh(stock)
        source = db.scalar(select(DataSourceState).where(DataSourceState.provider == "akshare"))
        assert result["updated"] == 1, result
        assert result["missing"] == 0, result
        assert result["errors"] == 0, result
        assert stock.quote_updated_at is not None
        assert source.last_quote_at is not None

        for item in db.scalars(select(Stock)).all():
            if item.symbol == "000001.SZ":
                item.change_pct = 4.9
                item.turnover_amount = 900_000_000
                item.last_price = 10.01
            else:
                item.change_pct = 0.2
                item.turnover_amount = 10_000_000
            db.add(item)
        db.commit()

        stock.quote_updated_at = None
        config = StrategyConfig(strategy_definition_id=definition.id, name="stale-check", mode="SIMULATION", parameters={})
        db.add(config)
        db.commit()
        run = execute_simulation_strategy(db, config)
        assert run.summary["accepted"] == 0
        assert "行情时间缺失" in run.summary["reason"]

    print("realtime_chain_ok")


if __name__ == "__main__":
    main()
