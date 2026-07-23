from __future__ import annotations

from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
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

    with pytest.raises(StaleDataError, match="未来"):
        ensure_fresh(
            "公司事件",
            updated_at=current + timedelta(seconds=1),
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
                return [
                    {
                        "代码": "000001",
                        "最新价": 12.34,
                        "涨跌幅": 1.25,
                        "成交额": 123456789,
                        "quote_at": datetime(
                            2026,
                            7,
                            10,
                            14,
                            40,
                            tzinfo=ZoneInfo("Asia/Shanghai"),
                        ),
                    }
                ]

        result = refresh_quotes(session, QuoteProvider(), ["000001.SZ"])
        stock = session.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        source = session.scalar(select(DataSourceState).where(DataSourceState.provider == "akshare"))

        assert result.updated == 1
        assert stock.last_price == 12.34
        assert stock.quote_updated_at.hour == 14
        assert stock.quote_updated_at.minute == 40
        assert source.healthy
        assert source.last_quote_at.hour == 14
        assert source.last_quote_at.minute == 40


def test_akshare_uses_index_history_for_csi300_benchmark():
    from app.market_data import AKShareProvider

    calls = []

    class Client:
        @staticmethod
        def index_zh_a_hist(**kwargs):
            calls.append(("index", kwargs))
            return [{"日期": "2026-07-22", "收盘": 4100}]

        @staticmethod
        def stock_zh_a_hist(**kwargs):
            calls.append(("stock", kwargs))
            return []

    provider = AKShareProvider()
    provider.client = Client()

    rows = provider.bars(
        symbol="000300.SH",
        timeframe="1d",
        start="2026-07-01",
        end="2026-07-22",
    )

    assert rows[0]["收盘"] == 4100
    assert [kind for kind, _ in calls] == ["index"]


def test_tushare_uses_index_daily_for_csi300_benchmark():
    from app.market_data import TushareProvider

    calls = []

    class Client:
        @staticmethod
        def index_daily(**kwargs):
            calls.append(("index", kwargs))
            return [{"trade_date": "20260722", "close": 4100}]

        @staticmethod
        def daily(**kwargs):
            calls.append(("stock", kwargs))
            return []

    provider = TushareProvider(token="")
    provider.client = Client()

    rows = provider.bars(
        symbol="000300.SH",
        timeframe="1d",
        start="2026-07-01",
        end="2026-07-22",
    )

    assert rows[0]["close"] == 4100
    assert [kind for kind, _ in calls] == ["index"]


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


def test_akshare_minute_bars_are_normalized_for_strategy_consumers():
    from app.market_data import AKShareProvider

    class Client:
        @staticmethod
        def stock_zh_a_hist_min_em(**_):
            return [
                {
                    "时间": "2026-07-23 14:39:00",
                    "开盘": 10.0,
                    "最高": 10.1,
                    "最低": 9.9,
                    "收盘": 10.05,
                    "成交量": 12_345,
                    "成交额": 1_240_000,
                }
            ]

    provider = AKShareProvider()
    provider.client = Client()

    rows = provider.bars(symbol="000001.SZ", timeframe="1m")

    assert rows == [
        {
            "symbol": "000001.SZ",
            "timestamp": "2026-07-23T14:39:00",
            "open": 10.0,
            "high": 10.1,
            "low": 9.9,
            "close": 10.05,
            "volume": 1_234_500,
            "amount": 1_240_000.0,
            "provider": "akshare",
        }
    ]


def test_tushare_minute_bars_are_normalized_for_strategy_consumers():
    from app.market_data import TushareProvider

    class Client:
        @staticmethod
        def stk_mins(**_):
            return [
                {
                    "trade_time": "2026-07-23 14:39:00",
                    "open": 10.0,
                    "high": 10.1,
                    "low": 9.9,
                    "close": 10.05,
                    "vol": 1_234_500,
                    "amount": 1_240,
                }
            ]

    provider = TushareProvider(token="")
    provider.client = Client()

    rows = provider.bars(symbol="000001.SZ", timeframe="1m")

    assert rows[0]["timestamp"] == "2026-07-23T14:39:00"
    assert rows[0]["volume"] == 1_234_500
    assert rows[0]["amount"] == 1_240_000
    assert rows[0]["provider"] == "tushare"


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


def test_mootdx_provider_reads_realtime_quotes_with_server_time():
    from app.market_data import MootdxProvider

    calls = []

    class Client:
        def quotes(self, *, symbol, market):
            calls.append((symbol, market))
            code = symbol[0]
            return [
                {
                    "code": code,
                    "price": 10.45,
                    "last_close": 10.49,
                    "open": 10.40,
                    "high": 10.60,
                    "low": 10.30,
                    "volume": 123_456,
                    "amount": 999_718_272,
                    "servertime": "15:29:53.736",
                }
            ]

    provider = MootdxProvider()
    provider.Quotes = object()
    provider.client = Client()
    provider.import_error = None

    rows = provider.quotes(["000001.SZ", "600519.SH"])

    assert "realtime" in provider.capabilities
    assert calls == [(["000001"], 0), (["600519"], 1)]
    assert [row["代码"] for row in rows] == ["000001", "600519"]
    assert rows[0]["最新价"] == 10.45
    assert rows[0]["涨跌幅"] == pytest.approx(-0.3813, abs=0.0001)
    assert rows[0]["成交额"] == 999_718_272
    assert rows[0]["open_price"] == 10.40
    assert rows[0]["high_price"] == 10.60
    assert rows[0]["low_price"] == 10.30
    assert rows[0]["volume"] == 12_345_600
    assert rows[0]["previous_close"] == 10.49
    assert rows[0]["quote_at"].strftime("%H:%M:%S") == "15:29:53"
    assert rows[0]["quote_at"].tzinfo.key == "Asia/Shanghai"


def test_mootdx_provider_normalizes_finance_float_shares_and_ipo_date():
    from app.market_data import MootdxProvider

    provider = MootdxProvider()
    provider.Quotes = object()
    provider.client = type(
        "Client",
        (),
        {
            "finance": lambda self, symbol: [
                {
                    "code": symbol,
                    "liutongguben": 19_405_601_250,
                    "ipo_date": 19910403,
                }
            ]
        },
    )()

    result = provider.finance("000001.SZ")

    assert result == {
        "float_shares": 19_405_601_250,
        "listing_date": "1991-04-03",
    }


def test_mootdx_provider_uses_one_client_per_worker_thread():
    from app.market_data import MootdxProvider

    created = []
    barrier = Barrier(2)

    class Quotes:
        @staticmethod
        def factory(*, market):
            client = object()
            created.append((market, client))
            return client

    provider = MootdxProvider()
    provider.Quotes = Quotes
    provider.client = None
    provider.import_error = None

    def resolve_twice():
        first = provider._quotes()
        barrier.wait(timeout=2)
        second = provider._quotes()
        return first, second

    with ThreadPoolExecutor(max_workers=2) as executor:
        pairs = list(executor.map(lambda _: resolve_twice(), range(2)))

    assert len(created) == 2
    assert all(first is second for first, second in pairs)
    assert pairs[0][0] is not pairs[1][0]


def test_akshare_event_provider_normalizes_real_announcements():
    from app.market_data import AKShareEventProvider

    class Client:
        @staticmethod
        def stock_notice_report(**kwargs):
            if kwargs != {"symbol": "全部", "date": "20260710"}:
                return []
            return [
                {
                    "代码": "000001",
                    "公告标题": "关于重大诉讼事项的公告",
                    "公告日期": "2026-07-10",
                    "网址": "https://example.test/AN202607101234567890.html",
                }
            ]

    provider = AKShareEventProvider(client=Client())
    rows = provider.events(
        symbols=["000001.SZ"],
        start="2026-07-03",
        end="2026-07-10",
    )

    assert rows[0]["source"] == "akshare"
    assert rows[0]["source_event_id"] == "AN202607101234567890"
    assert rows[0]["symbol"] == "000001.SZ"
    assert rows[0]["event_type"] == "material_litigation"
    assert rows[0]["published_at"].isoformat().startswith("2026-07-10")
    assert rows[0]["published_at"].tzinfo.key == "Asia/Shanghai"


def test_akshare_event_provider_extracts_unlock_percentage_from_title():
    from app.market_data import AKShareEventProvider

    class Client:
        @staticmethod
        def stock_notice_report(**kwargs):
            if kwargs["date"] != "20260710":
                return []
            return [
                {
                    "代码": "000001",
                    "公告标题": "限售股上市流通，占流通股本6.2%",
                    "公告日期": "2026-07-10",
                    "网址": "https://example.test/unlock.html",
                }
            ]

    rows = AKShareEventProvider(client=Client()).events(
        symbols=["000001.SZ"],
        start="2026-07-10",
        end="2026-07-10",
    )

    assert rows[0]["event_type"] == "unlock"
    assert rows[0]["unlock_free_float_pct"] == pytest.approx(0.062)


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
