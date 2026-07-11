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
    SimulationAccount,
    Stock,
    StockEvent,
    StrategyConfig,
    StrategyDefinition,
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
    assert quote_poll_scope(datetime(2026, 7, 14, 10, 0, 1, tzinfo=shanghai)) is None
    assert quote_poll_scope(datetime(2026, 7, 10, 20, 30, tzinfo=shanghai)) is None
    assert quote_poll_scope(datetime(2026, 7, 11, 14, 39, 40, tzinfo=shanghai)) is None


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
