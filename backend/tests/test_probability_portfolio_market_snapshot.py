from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import MarketDailyBar, Stock, StrategyConfig, StrategyDefinition
from app.probability_portfolio.market_snapshot import (
    calculate_intraday_factors,
    sync_probability_market_data,
)
from app.probability_portfolio.runtime import seed_probability_portfolio_runtime
from app.services import seed_database


SHANGHAI = ZoneInfo("Asia/Shanghai")
CURRENT = datetime(2026, 7, 23, 14, 40, 10, tzinfo=SHANGHAI)


def minute_rows(symbol: str) -> list[dict]:
    rows = []
    start = CURRENT.replace(hour=9, minute=30, second=0, microsecond=0)
    for index in range(312):
        timestamp = start + timedelta(minutes=index)
        price = 10 + index / 10_000
        rows.append(
            {
                "symbol": symbol,
                "timestamp": timestamp.isoformat(),
                "open": price,
                "high": price + 0.01,
                "low": price - 0.01,
                "close": price,
                "volume": 10_000,
                "amount": price * 10_000,
                "provider": "mootdx",
            }
        )
    rows.append(
        {
            **rows[-1],
            "timestamp": (CURRENT + timedelta(minutes=2)).isoformat(),
            "close": 99,
        }
    )
    return rows


def test_intraday_factors_ignore_current_and_future_bars():
    rows = minute_rows("000001.SZ")

    factors = calculate_intraday_factors(rows, current=CURRENT)

    completed = [
        row
        for row in rows
        if row["timestamp"] < CURRENT.replace(second=0, microsecond=0).isoformat()
    ]
    expected_vwap = sum(row["amount"] for row in completed) / sum(
        row["volume"] for row in completed
    )
    assert factors.vwap == pytest.approx(expected_vwap)
    assert factors.tail_30m_return == pytest.approx(
        completed[-1]["close"] / completed[-30]["close"] - 1
    )
    assert factors.last_completed_at.minute == 39
    assert factors.last_completed_at < CURRENT


def test_probability_snapshot_fetches_minutes_only_for_top_n_and_persists_factors(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshot.db'}")
    Base.metadata.create_all(engine)
    requested_minutes: list[str] = []
    requested_daily: list[str] = []
    requested_finance: list[str] = []
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        config.parameters = {**config.parameters, "prefilter_size": 2}
        db.commit()

    class Router:
        providers = []

        def call(self, capability, method, **kwargs):
            if capability == "realtime":
                rows = []
                for index, symbol in enumerate(kwargs["symbols"]):
                    rows.append(
                        {
                            "symbol": symbol,
                            "last_price": 10.5,
                            "previous_close": 10.2,
                            "change_pct": 3.0,
                            "turnover_amount": 900_000_000 - index * 10_000_000,
                            "turnover_rate": 0.02,
                            "open_price": 10.2,
                            "high_price": 10.6,
                            "low_price": 10.1,
                            "volume": 20_000_000,
                            "quote_at": CURRENT - timedelta(seconds=5),
                        }
                    )
                return SimpleNamespace(provider="quotes", data=rows)
            if capability == "daily":
                requested_daily.append(kwargs["symbol"])
                start = CURRENT.date() - timedelta(days=30)
                rows = [
                    {
                        "trade_date": (start + timedelta(days=index)).isoformat(),
                        "open": 10 + index / 100,
                        "high": 10.2 + index / 100,
                        "low": 9.8 + index / 100,
                        "close": 10.1 + index / 100,
                        "volume": 10_000_000,
                        "amount": 200_000_000,
                    }
                    for index in range(25)
                ]
                return SimpleNamespace(provider="daily", data=rows)
            if capability == "finance":
                requested_finance.append(kwargs["symbol"])
                return SimpleNamespace(
                    provider="mootdx",
                    data={
                        "float_shares": 1_000_000_000,
                        "listing_date": "2020-01-01",
                    },
                )
            requested_minutes.append(kwargs["symbol"])
            return SimpleNamespace(
                provider="mootdx",
                data=minute_rows(kwargs["symbol"]),
            )

    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "overnight_probability_portfolio"
            )
        )
        result = sync_probability_market_data(
            db,
            config,
            Router(),
            current=CURRENT,
        )
        selected = list(
            db.scalars(
                select(Stock)
                .where(Stock.symbol.in_(requested_minutes))
                .order_by(Stock.symbol)
            )
        )
        benchmark = db.scalar(select(Stock).where(Stock.symbol == "000300.SH"))
        benchmark_bars = list(
            db.scalars(
                select(MarketDailyBar).where(MarketDailyBar.stock_id == benchmark.id)
            )
        )

    assert len(requested_minutes) == 2
    assert len(requested_finance) == 2
    assert len(selected) == 2
    assert all(stock.vwap and stock.tail_30m_return is not None for stock in selected)
    assert all(stock.factor_updated_at is not None for stock in selected)
    assert all(
        stock.factor_updated_at == CURRENT.replace(tzinfo=None) for stock in selected
    )
    assert all(stock.listing_date == "2020-01-01" for stock in selected)
    assert "000300.SH" in requested_daily
    assert benchmark.status == "benchmark"
    assert len(benchmark_bars) >= 20
    assert result["minute_symbols"] == 2
    assert result["errors"] == 0


def test_probability_snapshot_reuses_complete_daily_cache(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshot-cache.db'}")
    Base.metadata.create_all(engine)
    requested_daily: list[str] = []
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        config.parameters = {**config.parameters, "prefilter_size": 1}
        benchmark = Stock(
            code="000300",
            exchange="SSE",
            symbol="000300.SH",
            name="沪深300",
            status="benchmark",
        )
        db.add(benchmark)
        db.flush()
        target = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        target.float_shares = 1_000_000_000
        target.listing_date = "2020-01-01"
        latest = CURRENT.date() - timedelta(days=1)
        for stock in (target, benchmark):
            for index in range(20):
                db.add(
                    MarketDailyBar(
                        stock_id=stock.id,
                        trade_date=(latest - timedelta(days=19 - index)).isoformat(),
                        open=10,
                        high=10.2,
                        low=9.8,
                        close=10 + index / 100,
                        volume=10_000_000,
                        amount=200_000_000,
                        source="cache",
                    )
                )
        db.commit()

    class Router:
        def call(self, capability, method, **kwargs):
            if capability == "realtime":
                return SimpleNamespace(
                    provider="quotes",
                    data=[
                        {
                            "symbol": symbol,
                            "last_price": 10.5,
                            "previous_close": 10.2,
                            "change_pct": 3,
                            "turnover_amount": (
                                900_000_000 if symbol == "000001.SZ" else 1
                            ),
                            "open_price": 10.2,
                            "high_price": 10.6,
                            "low_price": 10.1,
                            "volume": 20_000_000,
                            "quote_at": CURRENT - timedelta(seconds=5),
                        }
                        for symbol in kwargs["symbols"]
                    ],
                )
            if capability == "daily":
                requested_daily.append(kwargs["symbol"])
                raise AssertionError("完整日线缓存不得重复拉取")
            if capability == "finance":
                raise AssertionError("完整财务缓存不得重复拉取")
            return SimpleNamespace(
                provider="mootdx",
                data=minute_rows(kwargs["symbol"]),
            )

    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "overnight_probability_portfolio"
            )
        )
        result = sync_probability_market_data(
            db,
            config,
            Router(),
            current=CURRENT,
        )

    assert requested_daily == []
    assert result["daily_rows"] == 0
    assert result["errors"] == 0


def test_probability_snapshot_reports_per_stock_failures_without_retrying_batch(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'partial-failure.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        config.parameters = {**config.parameters, "prefilter_size": 2}
        db.commit()

    class Router:
        def call(self, capability, method, **kwargs):
            if capability == "realtime":
                return SimpleNamespace(
                    provider="quotes",
                    data=[
                        {
                            "symbol": symbol,
                            "last_price": 10.5,
                            "previous_close": 10.2,
                            "change_pct": 3,
                            "turnover_amount": 900_000_000 - index,
                            "open_price": 10.2,
                            "high_price": 10.6,
                            "low_price": 10.1,
                            "volume": 20_000_000,
                            "quote_at": CURRENT - timedelta(seconds=5),
                        }
                        for index, symbol in enumerate(kwargs["symbols"])
                    ],
                )
            if capability == "daily":
                return SimpleNamespace(provider="daily", data=[])
            if capability == "finance":
                if kwargs["symbol"] == "000001.SZ":
                    raise RuntimeError("财务接口暂不可用")
                return SimpleNamespace(
                    provider="finance",
                    data={"float_shares": 1_000_000_000, "listing_date": "2020-01-01"},
                )
            return SimpleNamespace(
                provider="minute",
                data=minute_rows(kwargs["symbol"]),
            )

    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "overnight_probability_portfolio"
            )
        )
        result = sync_probability_market_data(db, config, Router(), current=CURRENT)

        failed = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))

    assert result["errors"] == 0
    assert result["candidate_errors"] >= 1
    assert failed.factor_updated_at is None
