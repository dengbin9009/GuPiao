from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.data_sync import refresh_quotes
from app.database import Base
from app.models import MarketDailyBar, Stock
from app.probability_portfolio.features import build_feature_vector
from app.services import seed_database


SHANGHAI = ZoneInfo("Asia/Shanghai")
CURRENT = datetime(2026, 7, 23, 14, 40, tzinfo=SHANGHAI)


def daily_bars(stock_id: int, *, count: int = 21, end_day: int = 22):
    rows = []
    for index in range(count):
        day = datetime(2026, 7, end_day, tzinfo=SHANGHAI) - timedelta(days=count - index)
        close = 10 + index * 0.05
        rows.append(
            MarketDailyBar(
                stock_id=stock_id,
                trade_date=day.date().isoformat(),
                open=close - 0.03,
                high=close + 0.08,
                low=close - 0.08,
                close=close,
                volume=10_000_000,
                amount=200_000_000,
                source="test",
                captured_at=CURRENT - timedelta(days=1),
            )
        )
    return rows


def valid_stock() -> Stock:
    return Stock(
        id=1,
        code="000001",
        exchange="SZSE",
        symbol="000001.SZ",
        name="测试股份",
        status="active",
        listing_date="2020-01-01",
        last_price=11.20,
        change_pct=3.0,
        turnover_amount=220_000_000,
        turnover_rate=0.02,
        open_price=10.95,
        high_price=11.30,
        low_price=10.90,
        vwap=11.05,
        tail_30m_return=0.008,
        limit_up_price=12.0,
        limit_down_price=9.8,
        quote_source="mootdx",
        quote_updated_at=CURRENT - timedelta(seconds=5),
        factor_updated_at=CURRENT - timedelta(seconds=5),
    )


def test_complete_real_data_builds_probability_features():
    result = build_feature_vector(
        valid_stock(),
        daily_bars(1),
        daily_bars(2, count=6),
        current=CURRENT,
        source_healthy=True,
        critical_event=False,
        market_breadth=0.58,
    )

    assert result.accepted is True
    assert result.reasons == ()
    assert result.features["intraday_return"] == 0.03
    assert result.features["turnover_rate"] == 0.02
    assert result.features["vwap_distance"] > 0
    assert result.features["ma5_distance"] > 0
    assert result.features["ma20_distance"] > 0
    assert result.features["volatility_20d"] > 0
    assert result.features["market_breadth"] == 0.58


def test_future_bar_and_missing_core_fields_fail_closed():
    stock = valid_stock()
    stock.vwap = None
    stock.turnover_rate = None
    bars = daily_bars(1)
    bars.append(
        MarketDailyBar(
            stock_id=1,
            trade_date=CURRENT.date().isoformat(),
            open=12,
            high=13,
            low=11,
            close=12.5,
            volume=1,
            amount=1,
            source="future",
            captured_at=CURRENT,
        )
    )

    result = build_feature_vector(
        stock,
        bars,
        daily_bars(2, count=6),
        current=CURRENT,
        source_healthy=True,
        critical_event=False,
        market_breadth=0.5,
    )

    assert result.accepted is False
    assert "缺少真实日内VWAP" in result.reasons
    assert "缺少真实换手率" in result.reasons
    assert "日线包含未完成或未来数据" in result.reasons


def test_listing_limit_event_benchmark_and_stale_checks_reject():
    stock = valid_stock()
    stock.listing_date = "2026-07-01"
    stock.last_price = stock.limit_up_price
    stock.quote_updated_at = CURRENT - timedelta(minutes=3)
    benchmark = daily_bars(2, count=6)
    benchmark[-1].close = 8

    result = build_feature_vector(
        stock,
        daily_bars(1),
        benchmark,
        current=CURRENT,
        source_healthy=False,
        critical_event=True,
        market_breadth=0.2,
        max_quote_age_seconds=60,
    )

    assert result.accepted is False
    assert "上市时间不足60日" in result.reasons
    assert "股票处于涨停或跌停价格" in result.reasons
    assert "行情已过期" in result.reasons
    assert "行情来源不健康" in result.reasons
    assert "市场基准未通过MA5过滤" in result.reasons
    assert "命中重大事件风险" in result.reasons


def test_refresh_quotes_persists_probability_factor_fields(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'quotes.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=str(engine.url)))

        class Provider:
            name = "mootdx"

            def quotes(self, _symbols):
                return [
                    {
                        "代码": "000001",
                        "最新价": 11.2,
                        "涨跌幅": 3.0,
                        "成交额": 220_000_000,
                        "换手率": 2.0,
                        "今开": 10.95,
                        "最高": 11.3,
                        "最低": 10.9,
                        "成交量": 20_000_000,
                        "日内VWAP": 11.05,
                        "尾盘30分钟收益": 0.008,
                        "涨停价": 12.0,
                        "跌停价": 9.8,
                        "quote_at": CURRENT,
                    }
                ]

        refresh_quotes(db, Provider(), ["000001.SZ"])
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))

        assert stock.turnover_rate == 0.02
        assert stock.open_price == 10.95
        assert stock.high_price == 11.3
        assert stock.low_price == 10.9
        assert stock.vwap == 11.05
        assert stock.tail_30m_return == 0.008
        assert stock.limit_up_price == 12.0
        assert stock.limit_down_price == 9.8
        assert stock.quote_source == "mootdx"
        assert stock.factor_updated_at == CURRENT.replace(tzinfo=None)
