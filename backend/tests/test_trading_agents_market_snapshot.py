from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import MarketDailyBar, Position, Stock, StrategyConfig, StrategyDefinition
from app.services import seed_database
from app.trading_agents.market_snapshot import sync_agent_market_data
from app.trading_agents.runtime import seed_trading_agents_runtime


SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_market_snapshot_refreshes_top_turnover_and_holdings_only(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'market-snapshot.db'}")
    Base.metadata.create_all(engine)
    current = datetime(2026, 7, 14, 13, 25, tzinfo=SHANGHAI)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=str(engine.url)))
        seed_trading_agents_runtime(db, Settings(database_url=str(engine.url)))
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        config.parameters = {**config.parameters, "prefilter_size": 2}
        account_id = config.simulation_account_id
        stocks = list(
            db.scalars(
                select(Stock)
                .where(Stock.exchange.in_(["SSE", "SZSE"]))
                .order_by(Stock.symbol)
            )
        )
        holding = stocks[0]
        holding_symbol = holding.symbol
        db.add(
            Position(
                account_id=account_id,
                mode="SIMULATION",
                stock_id=holding.id,
                quantity=100,
                available_quantity=100,
                average_cost=10,
                market_value=1000,
                unrealized_pnl=0,
            )
        )
        db.commit()

    requested_daily = []

    class Router:
        def call(self, capability, method, **kwargs):
            if capability == "realtime":
                rows = []
                for index, symbol in enumerate(kwargs["symbols"]):
                    rows.append(
                        {
                            "symbol": symbol,
                            "last_price": 10 + index,
                            "change_pct": 1,
                            "amount": 100_000_000 + index * 100_000_000,
                            "quote_at": current,
                        }
                    )
                return SimpleNamespace(provider="test-quotes", data=rows)
            requested_daily.append(kwargs["symbol"])
            rows = []
            start = current.date() - timedelta(days=90)
            for index in range(70):
                day = start + timedelta(days=index)
                rows.append(
                    {
                        "trade_date": day.isoformat(),
                        "open": 10,
                        "high": 11,
                        "low": 9,
                        "close": 10 + index / 100,
                        "volume": 10_000_000,
                        "amount": 200_000_000,
                    }
                )
            rows.append({**rows[-1], "trade_date": current.date().isoformat()})
            return SimpleNamespace(provider="test-daily", data=rows)

    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        result = sync_agent_market_data(db, config, Router(), current=current)
        today_count = db.scalar(
            select(func.count(MarketDailyBar.id)).where(
                MarketDailyBar.trade_date == current.date().isoformat()
            )
        )

    assert result["quote_errors"] == 0
    assert result["daily_symbols"] == 3
    assert len(set(requested_daily)) == 3
    assert holding_symbol in requested_daily
    assert today_count == 0


def test_market_snapshot_reports_partial_daily_failures_for_same_day_retry(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'market-errors.db'}")
    Base.metadata.create_all(engine)
    current = datetime(2026, 7, 14, 13, 25, tzinfo=SHANGHAI)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_trading_agents_runtime(db, settings)
        config.parameters = {**config.parameters, "prefilter_size": 1}
        db.commit()

    class Router:
        def call(self, capability, method, **kwargs):
            if capability == "realtime":
                return SimpleNamespace(
                    provider="test-quotes",
                    data=[
                        {
                            "symbol": symbol,
                            "last_price": 10,
                            "change_pct": 1,
                            "amount": 200_000_000,
                            "quote_at": current,
                        }
                        for symbol in kwargs["symbols"]
                    ],
                )
            raise RuntimeError("daily source unavailable")

    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        result = sync_agent_market_data(db, config, Router(), current=current)

    assert result["daily_errors"] == 1
    assert result["errors"] == 1


def test_market_snapshot_retries_transient_daily_failure_before_marking_error(
    tmp_path: Path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'market-retry.db'}")
    Base.metadata.create_all(engine)
    current = datetime(2026, 7, 14, 13, 25, tzinfo=SHANGHAI)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_trading_agents_runtime(db, settings)
        config.parameters = {**config.parameters, "prefilter_size": 1}
        db.commit()

    attempts = 0

    class Router:
        def call(self, capability, method, **kwargs):
            nonlocal attempts
            if capability == "realtime":
                return SimpleNamespace(
                    provider="test-quotes",
                    data=[
                        {
                            "symbol": symbol,
                            "last_price": 10,
                            "change_pct": 1,
                            "amount": 200_000_000,
                            "quote_at": current,
                        }
                        for symbol in kwargs["symbols"]
                    ],
                )
            attempts += 1
            if attempts == 1:
                raise ConnectionError("remote end closed connection")
            start = current.date() - timedelta(days=90)
            rows = [
                {
                    "trade_date": (start + timedelta(days=index)).isoformat(),
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10 + index / 100,
                    "volume": 10_000_000,
                    "amount": 200_000_000,
                }
                for index in range(70)
            ]
            return SimpleNamespace(provider="test-daily", data=rows)

    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        result = sync_agent_market_data(
            db,
            config,
            Router(),
            current=current,
        )

    assert attempts == 2
    assert result["daily_errors"] == 0
    assert result["errors"] == 0
    assert result["daily_rows"] >= 60


def test_market_snapshot_reuses_fresh_daily_cache_without_refetching(
    tmp_path: Path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'market-cache.db'}")
    Base.metadata.create_all(engine)
    current = datetime(2026, 7, 14, 13, 25, tzinfo=SHANGHAI)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_trading_agents_runtime(db, settings)
        config.parameters = {**config.parameters, "prefilter_size": 1}
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        latest_completed = current.date() - timedelta(days=1)
        for index in range(60):
            trade_date = latest_completed - timedelta(days=59 - index)
            db.add(
                MarketDailyBar(
                    stock_id=stock.id,
                    trade_date=trade_date.isoformat(),
                    open=10,
                    high=11,
                    low=9,
                    close=10 + index / 100,
                    volume=10_000_000,
                    amount=200_000_000,
                    source="cached",
                )
            )
        db.commit()

    requested_daily = []

    class Router:
        def call(self, capability, method, **kwargs):
            if capability == "realtime":
                return SimpleNamespace(
                    provider="test-quotes",
                    data=[
                        {
                            "symbol": symbol,
                            "last_price": 10,
                            "change_pct": 1,
                            "amount": 500_000_000 if symbol == "000001.SZ" else 200_000_000,
                            "quote_at": current,
                        }
                        for symbol in kwargs["symbols"]
                    ],
                )
            requested_daily.append(kwargs["symbol"])
            raise AssertionError("fresh cache must not request daily data")

    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        result = sync_agent_market_data(
            db,
            config,
            Router(),
            current=current,
        )

    assert requested_daily == []
    assert result["daily_errors"] == 0
    assert result["daily_rows"] == 0
