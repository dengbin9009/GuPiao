from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest


class FakeProvider:
    def __init__(self, name: str, capabilities: set[str], healthy: bool, value=None):
        self.name = name
        self.capabilities = frozenset(capabilities)
        self._healthy = healthy
        self.value = value

    def health(self):
        return self._healthy, None if self._healthy else "offline"

    def bars(self, **_):
        return self.value


def test_router_falls_back_to_healthy_provider():
    from app.market_data import ProviderRouter

    primary = FakeProvider("akshare", {"minute"}, False)
    fallback = FakeProvider("tushare", {"minute"}, True, [{"close": 10.2}])

    result = ProviderRouter([primary, fallback]).call("minute", "bars", symbol="000001.SZ")

    assert result.provider == "tushare"
    assert result.data == [{"close": 10.2}]


def test_router_can_fall_back_to_mootdx_provider():
    from app.market_data import ProviderRouter

    primary = FakeProvider("akshare", {"minute"}, False)
    secondary = FakeProvider("tushare", {"minute"}, False)
    fallback = FakeProvider("mootdx", {"minute"}, True, [{"close": 10.8}])

    result = ProviderRouter([primary, secondary, fallback]).call("minute", "bars", symbol="000001.SZ")

    assert result.provider == "mootdx"
    assert result.data == [{"close": 10.8}]


def test_corporate_events_are_normalized_and_deduplicated():
    from app.market_data import normalize_events

    rows = [
        {"source": "cninfo", "source_event_id": "A1", "symbol": "000001.SZ", "title": "停牌", "event_type": "suspension"},
        {"source": "cninfo", "source_event_id": "A1", "symbol": "000001.SZ", "title": "停牌重复", "event_type": "suspension"},
        {"source": "tushare", "source_event_id": "B2", "symbol": "000001.SZ", "title": "业绩预警", "event_type": "earnings_warning"},
    ]

    normalized = normalize_events(rows)

    assert len(normalized) == 2
    assert normalized[0]["severity"] == "critical"
    assert normalized[1]["severity"] == "warning"


def test_stale_event_data_fails_closed():
    from app.market_data import StaleDataError, ensure_fresh

    current = datetime(2026, 6, 23, 14, 40, tzinfo=ZoneInfo("Asia/Shanghai"))

    with pytest.raises(StaleDataError, match="公司事件"):
        ensure_fresh(
            "公司事件",
            updated_at=current - timedelta(seconds=1801),
            stale_after_seconds=1800,
            current=current,
        )


def test_akshare_quote_refresh_updates_timestamp(tmp_path):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.data_sync import refresh_quotes
    from app.database import Base
    from app.models import DataSourceState, Stock

    engine = create_engine(f"sqlite:///{tmp_path / 'quotes.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Stock(
                code="000001",
                exchange="SZSE",
                symbol="000001.SZ",
                name="平安银行",
                status="active",
            )
        )
        session.add(DataSourceState(provider="akshare", enabled=True, healthy=False, capabilities=["realtime"]))
        session.commit()

        class QuoteProvider:
            name = "akshare"

            def quotes(self, symbols):
                assert symbols == ["000001.SZ"]
                return [{"代码": "000001", "最新价": 12.34, "涨跌幅": 1.25, "成交额": 123456789}]

        result = refresh_quotes(session, QuoteProvider(), ["000001.SZ"])
        stock = session.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        source = session.scalar(select(DataSourceState).where(DataSourceState.provider == "akshare"))

        assert result.updated == 1
        assert stock.last_price == 12.34
        assert stock.quote_updated_at is not None
        assert source.healthy
        assert source.last_quote_at is not None


def test_stock_master_sync_generates_pinyin_metadata(tmp_path):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.data_sync import sync_stock_master
    from app.database import Base
    from app.models import DataSourceState, Stock

    engine = create_engine(f"sqlite:///{tmp_path / 'master.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(DataSourceState(provider="tushare", enabled=True, healthy=False, capabilities=["stock_master"]))
        session.commit()

        class MasterProvider:
            name = "tushare"

            def stock_master(self):
                return [{"ts_code": "600519.SH", "name": "贵州茅台"}]

        result = sync_stock_master(session, MasterProvider())
        stock = session.scalar(select(Stock).where(Stock.symbol == "600519.SH"))

        assert result.created == 1
        assert stock.pinyin == "guizhoumaotai"
        assert stock.pinyin_initials == "gzmt"


def test_router_falls_back_for_stock_master_too():
    from app.market_data import ProviderRouter

    class MasterProvider(FakeProvider):
        def stock_master(self):
            return self.value

    primary = MasterProvider("akshare", {"stock_master"}, False)
    fallback = MasterProvider("tushare", {"stock_master"}, True, [{"ts_code": "600519.SH", "name": "贵州茅台"}])

    result = ProviderRouter([primary, fallback]).call("stock_master", "stock_master")

    assert result.provider == "tushare"
    assert result.data[0]["name"] == "贵州茅台"


def test_tushare_provider_exposes_trading_days_when_client_available():
    from app.market_data import TushareProvider

    provider = TushareProvider(token="")
    provider.client = type(
        "Client",
        (),
        {
            "trade_cal": lambda self, **_: [
                {"cal_date": "2026-06-23"},
                {"cal_date": "2026-06-24"},
            ]
        },
    )()
    provider.import_error = None

    days = provider.trading_days(start="2026-06-23", end="2026-06-24")

    assert days == ["2026-06-23", "2026-06-24"]


def test_mootdx_provider_reads_minute_bars_when_client_available():
    from app.market_data import MootdxProvider

    provider = MootdxProvider()
    provider.Quotes = object()
    provider.client = type(
        "Client",
        (),
        {
            "bars": lambda self, symbol, market, frequency: [
                {"datetime": "2026-06-24 14:45:00", "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.05, "volume": 10000, "amount": 100500}
            ]
        },
    )()
    provider.import_error = None

    rows = provider.bars(symbol="000001.SZ", timeframe="1m")

    assert rows[0]["timestamp"].startswith("2026-06-24T14:45:00")
    assert rows[0]["close"] == 10.05
    assert rows[0]["provider"] == "mootdx"


def test_mootdx_provider_does_not_advertise_unsupported_trading_calendar():
    from app.market_data import MootdxProvider

    provider = MootdxProvider()

    assert "trading_calendar" not in provider.capabilities


def test_corporate_event_sync_creates_records(tmp_path):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.data_sync import sync_corporate_events
    from app.database import Base
    from app.models import Stock, StockEvent

    engine = create_engine(f"sqlite:///{tmp_path / 'events.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Stock(
                code="000001",
                exchange="SZSE",
                symbol="000001.SZ",
                name="平安银行",
                status="active",
            )
        )
        session.commit()

        result = sync_corporate_events(
            session,
            [
                {
                    "source": "cninfo",
                    "source_event_id": "evt-1",
                    "symbol": "000001.SZ",
                    "title": "重大事项停牌",
                    "event_type": "suspension",
                }
            ],
        )
        event = session.scalar(select(StockEvent).where(StockEvent.source_event_id == "evt-1"))

        assert result.created == 1
        assert event is not None
        assert event.severity == "critical"
