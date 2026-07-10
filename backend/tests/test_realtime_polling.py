from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import DataSourceState, Stock, StrategyConfig, StrategyDefinition, WatchlistItem
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
