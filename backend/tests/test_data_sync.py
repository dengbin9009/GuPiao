from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import Stock, StockEvent
from app.services import seed_database


@pytest.fixture()
def db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sync.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed_database(session, Settings(database_url=f"sqlite:///{tmp_path / 'sync.db'}"))
        yield session


class StockProvider:
    name = "test"

    def stock_master(self):
        return [{"ts_code": "601318.SH", "symbol": "601318", "name": "中国平安", "exchange": "SSE"}]

    def quotes(self, symbols):
        return [{"symbol": symbols[0], "last_price": 13.2, "change_pct": 1.1, "amount": 123_000_000}]


def test_stock_master_sync_generates_search_metadata(db: Session):
    from app.data_sync import sync_stock_master

    result = sync_stock_master(db, StockProvider(), pinyin_resolver=lambda _: ("zhongguopingan", "zgpa"))

    stock = db.scalar(select(Stock).where(Stock.symbol == "601318.SH"))
    assert result.created == 1
    assert stock is not None
    assert stock.code == "601318"
    assert stock.pinyin == "zhongguopingan"
    assert stock.pinyin_initials == "zgpa"


def test_quote_refresh_marks_requested_missing_symbol(db: Session):
    from app.data_sync import refresh_quotes

    first = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    missing = db.scalar(select(Stock).where(Stock.symbol == "000858.SZ"))

    result = refresh_quotes(db, StockProvider(), [first.symbol, missing.symbol])

    db.refresh(first)
    db.refresh(missing)
    assert first.last_price == 13.2
    assert result.missing == ["000858.SZ"]
    assert missing.last_price is None
    assert missing.quote_updated_at is None


def test_event_sync_deduplicates_source_event_id(db: Session):
    from app.data_sync import sync_corporate_events

    rows = [
        {"source": "cninfo", "source_event_id": "A1", "symbol": "000001.SZ", "title": "停牌", "event_type": "suspension"},
        {"source": "cninfo", "source_event_id": "A1", "symbol": "000001.SZ", "title": "停牌重复", "event_type": "suspension"},
    ]

    result = sync_corporate_events(db, rows)

    assert result.created == 1
    assert len(list(db.scalars(select(StockEvent)))) == 1


def test_chinese_quote_units_are_normalized_and_price_limits_are_derived(db: Session):
    from app.data_sync import refresh_quotes

    stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))

    class Provider:
        name = "akshare"

        def quotes(self, _symbols):
            return [
                {
                    "代码": "000001",
                    "最新价": 11.08,
                    "昨收": 10.98,
                    "今开": 10.92,
                    "最高": 11.12,
                    "最低": 10.90,
                    "成交量": 1_095_742,
                    "成交额": 1_210_838_016,
                    "换手率": 0.56,
                }
            ]

    refresh_quotes(db, Provider(), [stock.symbol])

    assert stock.volume == 109_574_200
    assert stock.turnover_rate == pytest.approx(0.0056)
    assert stock.vwap == pytest.approx(1_210_838_016 / 109_574_200)
    assert stock.limit_up_price == 12.08
    assert stock.limit_down_price == 9.88
    assert stock.factor_updated_at is None


def test_quote_refresh_preserves_same_day_intraday_factors_and_clears_old_day(
    db: Session,
):
    from app.data_sync import refresh_quotes

    stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    factor_at = datetime(2026, 7, 23, 14, 40, tzinfo=ZoneInfo("Asia/Shanghai"))
    stock.vwap = 11.01
    stock.tail_30m_return = 0.008
    stock.factor_updated_at = factor_at
    stock.float_shares = 10_000_000_000
    db.commit()

    class Provider:
        name = "mootdx"

        def __init__(self, quote_at):
            self.quote_at = quote_at

        def quotes(self, _symbols):
            return [
                {
                    "代码": "000001",
                    "最新价": 11.08,
                    "涨跌幅": 1.0,
                    "成交额": 1_210_838_016,
                    "volume": 109_574_200,
                    "quote_at": self.quote_at,
                }
            ]

    refresh_quotes(
        db,
        Provider(factor_at + timedelta(seconds=5)),
        [stock.symbol],
    )
    assert stock.tail_30m_return == 0.008
    assert stock.factor_updated_at == factor_at.replace(tzinfo=None)
    assert stock.turnover_rate == pytest.approx(109_574_200 / 10_000_000_000)

    refresh_quotes(
        db,
        Provider(factor_at + timedelta(days=1)),
        [stock.symbol],
    )
    assert stock.tail_30m_return is None
    assert stock.factor_updated_at is None
