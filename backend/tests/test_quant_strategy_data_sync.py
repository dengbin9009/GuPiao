from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.market_data import AKShareProvider, ProviderRouter, TushareProvider
from app.models import (
    DataSourceState,
    FinancialReportSnapshot,
    MarketDailyBar,
    MarketDailyMetric,
    Position,
    NotificationChannel,
    NotificationDelivery,
    Stock,
)
from app.quant_strategies.data_sync import (
    configured_etf_symbols,
    financial_available_on,
    quant_sync_stock_universe,
    sync_adjustment_rows,
    sync_daily_rows,
    sync_etf_master_rows,
    sync_financial_rows,
    sync_metric_rows,
)
from app.quant_strategies.runtime import seed_quant_strategy_runtimes
from app.services import seed_database


class Frame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orient):
        assert orient == "records"
        return self.rows


def test_quant_dataset_upserts_preload_existing_rows_in_bulk(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bulk-upsert.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        stock = Stock(
            code="000001",
            exchange="SZSE",
            symbol="000001.SZ",
            name="平安银行",
            status="active",
            instrument_type="STOCK",
        )
        db.add(stock)
        db.commit()
        dates = [f"2026-07-{day:02d}" for day in range(1, 21)]
        select_statements = []

        def record_select(_conn, _cursor, statement, *_args):
            if statement.lstrip().upper().startswith("SELECT"):
                select_statements.append(statement)

        event.listen(engine, "before_cursor_execute", record_select)
        try:
            sync_daily_rows(
                db,
                stock,
                [
                    {
                        "trade_date": trade_date,
                        "open": 10,
                        "high": 11,
                        "low": 9,
                        "close": 10,
                        "volume": 100,
                        "amount": 200_000_000,
                    }
                    for trade_date in dates
                ],
                source="akshare",
            )
            sync_adjustment_rows(
                db,
                stock,
                [
                    {"trade_date": trade_date, "adjustment_factor": 1.2}
                    for trade_date in dates
                ],
                source="akshare",
            )
            sync_metric_rows(
                db,
                stock,
                [
                    {"trade_date": trade_date, "pe_ttm": 10, "pb": 1}
                    for trade_date in dates
                ],
                source="akshare",
            )
            sync_financial_rows(
                db,
                stock,
                [
                    {
                        "report_period": "2025-12-31",
                        "actual_announcement_date": "2026-03-20",
                        "eps": 1,
                    },
                    {
                        "report_period": "2026-03-31",
                        "actual_announcement_date": "2026-04-25",
                        "eps": 0.3,
                    },
                ],
                source="akshare",
                trading_days={"2026-03-23", "2026-04-27"},
            )
        finally:
            event.remove(engine, "before_cursor_execute", record_select)

        assert len(select_statements) <= 4


class FakeTushare:
    def adj_factor(self, **_kwargs):
        return Frame([{"trade_date": "20260723", "adj_factor": 1.25}])

    def daily_basic(self, **_kwargs):
        return Frame(
            [
                {
                    "trade_date": "20260723",
                    "pe_ttm": 12.5,
                    "pb": 1.2,
                    "dv_ttm": 2.0,
                    "total_mv": 1000,
                    "circ_mv": 800,
                }
            ]
        )

    def fina_indicator(self, **_kwargs):
        return Frame(
            [
                {
                    "end_date": "20260630",
                    "ann_date": "20260830",
                    "eps": 1.1,
                    "roe": 18.0,
                    "grossprofit_margin": 35.0,
                }
            ]
        )

    def cashflow(self, **_kwargs):
        return Frame(
            [
                {
                    "end_date": "20260630",
                    "f_ann_date": "20260830",
                    "n_cashflow_act": 20,
                }
            ]
        )

    def income(self, **_kwargs):
        return Frame(
            [
                {
                    "end_date": "20260630",
                    "f_ann_date": "20260830",
                    "n_income": 10,
                    "revenue": 100,
                }
            ]
        )

    def balancesheet(self, **_kwargs):
        return Frame(
            [
                {
                    "end_date": "20260630",
                    "f_ann_date": "20260830",
                    "total_assets": 200,
                    "total_liab": 60,
                }
            ]
        )

    def fund_basic(self, **_kwargs):
        return Frame(
            [
                {
                    "ts_code": "510300.SH",
                    "name": "沪深300ETF",
                    "market": "E",
                    "list_date": "20120528",
                }
            ]
        )

    def fund_daily(self, **_kwargs):
        return Frame(
            [
                {
                    "trade_date": "20260723",
                    "open": 4.0,
                    "high": 4.1,
                    "low": 3.9,
                    "close": 4.05,
                    "vol": 1000,
                    "amount": 4000,
                }
            ]
        )


def provider() -> TushareProvider:
    result = object.__new__(TushareProvider)
    result.token = "configured"
    result.client = FakeTushare()
    result.import_error = None
    return result


def test_tushare_exposes_quant_and_etf_capabilities():
    source = provider()

    assert {"adjustment", "daily_metric", "financial", "etf_master", "etf_daily"} <= source.capabilities
    assert source.adjustment_factors("000001.SZ", start="2026-07-01", end="2026-07-23")[0]["adjustment_factor"] == 1.25
    assert source.daily_metrics("000001.SZ", start="2026-07-01", end="2026-07-23")[0]["dividend_yield"] == 0.02
    assert source.financial_reports("000001.SZ")[0]["operating_cash_flow"] == 20
    assert source.etf_master()[0]["instrument_type"] == "ETF"
    assert source.etf_bars("510300.SH", start="2026-07-01", end="2026-07-23")[0]["close"] == 4.05


def test_akshare_exposes_point_in_time_metrics_and_financial_reports():
    calls = []

    class Client:
        @staticmethod
        def stock_zh_valuation_baidu(**kwargs):
            calls.append(("metric", kwargs))
            values = {
                "市盈率(TTM)": 12.5,
                "市净率": 1.2,
                "总市值": 2500,
            }
            return Frame(
                [
                    {"date": "2026-07-24", "value": values[kwargs["indicator"]]},
                    {"date": "2026-07-23", "value": values[kwargs["indicator"]] - 0.1},
                ]
            )

        @staticmethod
        def stock_financial_analysis_indicator_em(**kwargs):
            calls.append(("financial", kwargs))
            return Frame(
                [
                    {
                        "REPORT_DATE": "2026-03-31 00:00:00",
                        "NOTICE_DATE": "2026-04-25 00:00:00",
                        "REPORT_TYPE": "一季报",
                        "EPSJB": 0.67,
                        "ROEJQ": 2.83,
                        "XSMLL": 35.0,
                        "TOTALOPERATEREVE": 35_277_000_000,
                        "PARENTNETPROFIT": 14_523_000_000,
                        "JYXJLYYSR": 1.0715,
                        "LIABILITY": 5_489_879_000_000,
                        "ZCFZL": 90.9829892863,
                    }
                ]
            )

    source = AKShareProvider()
    source.client = Client()

    metrics = source.daily_metrics(
        "000001.SZ",
        start="2026-07-23",
        end="2026-07-24",
    )
    reports = source.financial_reports("000001.SZ")

    assert {"adjustment", "daily_metric", "financial"} <= source.capabilities
    assert metrics[-1] == {
        "trade_date": "2026-07-24",
        "pe_ttm": 12.5,
        "pb": 1.2,
        "total_market_value": 250_000_000_000.0,
    }
    assert reports[0]["report_period"] == "2026-03-31"
    assert reports[0]["actual_announcement_date"] == "2026-04-25"
    assert reports[0]["roe"] == pytest.approx(0.0283)
    assert reports[0]["gross_margin"] == pytest.approx(0.35)
    assert reports[0]["operating_cash_flow"] == pytest.approx(
        35_277_000_000 * 0.010715
    )
    assert reports[0]["total_assets"] == pytest.approx(
        5_489_879_000_000 / 0.909829892863
    )
    assert [name for name, _kwargs in calls] == [
        "metric",
        "metric",
        "metric",
        "financial",
    ]


def test_tushare_normalizes_daily_cross_sections_for_batch_incremental_sync():
    calls = []

    class Client(FakeTushare):
        def daily(self, **kwargs):
            calls.append(("daily", kwargs))
            return Frame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20260724",
                        "open": 10,
                        "high": 11,
                        "low": 9,
                        "close": 10.5,
                        "vol": 1000,
                        "amount": 2000,
                    }
                ]
            )

        def adj_factor(self, **kwargs):
            calls.append(("adjustment", kwargs))
            return Frame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20260724",
                        "adj_factor": 1.25,
                    }
                ]
            )

        def daily_basic(self, **kwargs):
            calls.append(("metric", kwargs))
            return Frame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20260724",
                        "pe_ttm": 12.5,
                        "pb": 1.2,
                        "dv_ttm": 2.0,
                        "total_mv": 1000,
                        "circ_mv": 800,
                    }
                ]
            )

    source = provider()
    source.client = Client()

    daily = source.daily_cross_section("2026-07-24")
    adjustments = source.adjustment_cross_section("2026-07-24")
    metrics = source.daily_metric_cross_section("2026-07-24")

    assert daily[0] == {
        "symbol": "000001.SZ",
        "trade_date": "2026-07-24",
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "volume": 100_000.0,
        "amount": 2_000_000.0,
    }
    assert adjustments[0] == {
        "symbol": "000001.SZ",
        "trade_date": "2026-07-24",
        "adjustment_factor": 1.25,
    }
    assert metrics[0]["symbol"] == "000001.SZ"
    assert metrics[0]["dividend_yield"] == 0.02
    assert [kwargs for _name, kwargs in calls] == [
        {"trade_date": "20260724"},
        {"trade_date": "20260724"},
        {"ts_code": "", "trade_date": "20260724"},
    ]


def test_tushare_financial_reports_keep_separate_announcement_versions():
    source = provider()
    source.client.fina_indicator = lambda **_kwargs: Frame(
        [
            {
                "end_date": "20260630",
                "ann_date": "20260830",
                "eps": 1.0,
                "roe": 15,
            },
            {
                "end_date": "20260630",
                "ann_date": "20260915",
                "eps": 1.2,
                "roe": 17,
            },
        ]
    )
    source.client.cashflow = lambda **_kwargs: Frame([])
    source.client.income = lambda **_kwargs: Frame([])
    source.client.balancesheet = lambda **_kwargs: Frame([])

    rows = source.financial_reports("000001.SZ")

    assert [(row["actual_announcement_date"], row["eps"]) for row in rows] == [
        ("2026-08-30", 1.0),
        ("2026-09-15", 1.2),
    ]


def test_tushare_financial_cross_section_merges_four_vip_datasets_by_symbol():
    calls = []

    class Client:
        def fina_indicator_vip(self, **kwargs):
            calls.append(("fina_indicator_vip", kwargs))
            return Frame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "end_date": kwargs["period"],
                        "ann_date": "20260830",
                        "eps": 1.1,
                        "roe": 18,
                        "grossprofit_margin": 35,
                    }
                ]
            )

        def cashflow_vip(self, **kwargs):
            calls.append(("cashflow_vip", kwargs))
            return Frame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "end_date": kwargs["period"],
                        "f_ann_date": "20260830",
                        "n_cashflow_act": 20,
                    }
                ]
            )

        def income_vip(self, **kwargs):
            calls.append(("income_vip", kwargs))
            return Frame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "end_date": kwargs["period"],
                        "f_ann_date": "20260830",
                        "n_income": 10,
                        "revenue": 100,
                    }
                ]
            )

        def balancesheet_vip(self, **kwargs):
            calls.append(("balancesheet_vip", kwargs))
            return Frame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "end_date": kwargs["period"],
                        "f_ann_date": "20260830",
                        "total_assets": 200,
                        "total_liab": 60,
                    }
                ]
            )

    source = provider()
    source.client = Client()

    rows = source.financial_report_cross_sections(["2026-06-30"])

    assert rows == [
        {
            "symbol": "000001.SZ",
            "report_period": "2026-06-30",
            "announcement_date": "2026-08-30",
            "actual_announcement_date": "2026-08-30",
            "eps": 1.1,
            "roe": 0.18,
            "gross_margin": 0.35000000000000003,
            "operating_cash_flow": 20.0,
            "net_profit": 10.0,
            "revenue": 100.0,
            "total_assets": 200.0,
            "total_liabilities": 60.0,
        }
    ]
    assert [name for name, _kwargs in calls] == [
        "fina_indicator_vip",
        "cashflow_vip",
        "income_vip",
        "balancesheet_vip",
    ]
    assert all(kwargs == {"period": "20260630"} for _name, kwargs in calls)


def test_akshare_exposes_etf_and_point_in_time_quant_fallback_capabilities():
    source = object.__new__(AKShareProvider)
    source.client = type(
        "FakeAkshare",
        (),
        {
            "fund_etf_spot_em": lambda self: Frame(
                [{"代码": "510300", "名称": "沪深300ETF"}]
            ),
            "fund_etf_hist_em": lambda self, **kwargs: Frame(
                [
                    {
                        "日期": "2026-07-23",
                        "开盘": 4,
                        "最高": 4.1,
                        "最低": 3.9,
                        "收盘": 4.05,
                        "成交量": 100,
                        "成交额": 4000,
                    }
                ]
            ),
        },
    )()
    source.import_error = None

    assert {
        "adjustment",
        "daily_metric",
        "financial",
        "etf_master",
        "etf_daily",
    } <= source.capabilities
    assert source.etf_master()[0]["ts_code"] == "510300.SH"
    assert source.etf_bars(
        "510300.SH",
        start="2026-07-01",
        end="2026-07-23",
    )[0]["close"] == 4.05


@pytest.mark.parametrize(
    ("announcement", "expected"),
    [
        ("2026-08-28", "2026-08-31"),
        ("2026-08-30", "2026-08-31"),
        ("2026-08-31", "2026-09-01"),
    ],
)
def test_financial_date_only_becomes_visible_next_weekday(announcement, expected):
    assert financial_available_on(announcement) == expected


def test_financial_date_only_uses_next_exchange_trading_day():
    trading_days = {"2026-10-09", "2026-10-12"}

    assert financial_available_on(
        "2026-09-30",
        trading_days=trading_days,
    ) == "2026-10-09"


def test_financial_sync_skips_reports_before_exchange_calendar_coverage(
    tmp_path: Path,
):
    database_url = f"sqlite:///{tmp_path / 'financial-window.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        stock = Stock(
            code="000001",
            exchange="SZSE",
            symbol="000001.SZ",
            name="平安银行",
            status="active",
        )
        db.add(stock)
        db.commit()
        sync_financial_rows(
            db,
            stock,
            [
                {
                    "report_period": "2010-12-31",
                    "actual_announcement_date": "2011-03-01",
                    "eps": 0.1,
                },
                {
                    "report_period": "2026-03-31",
                    "actual_announcement_date": "2026-04-25",
                    "eps": 0.67,
                },
            ],
            source="akshare",
            trading_days={"2026-04-27", "2026-04-28"},
        )

        rows = list(db.scalars(select(FinancialReportSnapshot)))
        assert [(row.report_period, row.available_on) for row in rows] == [
            ("2026-03-31", "2026-04-27")
        ]


def test_quant_rows_are_upserted_without_deleting_history(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sync.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        stock = Stock(
            code="000001",
            exchange="SZSE",
            symbol="000001.SZ",
            name="平安银行",
            instrument_type="STOCK",
        )
        db.add(stock)
        db.flush()
        db.add(
            MarketDailyBar(
                stock_id=stock.id,
                trade_date="2026-07-23",
                open=10,
                high=11,
                low=9,
                close=10.5,
                volume=100,
                amount=1000,
                source="tushare",
            )
        )
        db.commit()

        sync_adjustment_rows(
            db,
            stock,
            [{"trade_date": "2026-07-23", "adjustment_factor": 1.25}],
            source="tushare",
        )
        sync_metric_rows(
            db,
            stock,
            [
                {
                    "trade_date": "2026-07-23",
                    "pe_ttm": 12.5,
                    "pb": 1.2,
                    "dividend_yield": 0.02,
                }
            ],
            source="tushare",
        )
        sync_financial_rows(
            db,
            stock,
            [
                {
                    "report_period": "2026-06-30",
                    "announcement_date": "2026-08-30",
                    "actual_announcement_date": "2026-08-30",
                    "eps": 1.1,
                    "roe": 0.18,
                    "gross_margin": 0.35,
                    "operating_cash_flow": 20,
                    "total_assets": 200,
                    "total_liabilities": 60,
                }
            ],
            source="tushare",
        )
        sync_metric_rows(
            db,
            stock,
            [{"trade_date": "2026-07-23", "pe_ttm": 13.0, "pb": 1.3}],
            source="tushare",
        )

        bar = db.scalar(select(MarketDailyBar))
        metric = db.scalar(select(MarketDailyMetric))
        report = db.scalar(select(FinancialReportSnapshot))

        assert bar.adjustment_factor == 1.25
        assert bar.source == "tushare"
        assert metric.pe_ttm == 13.0
        assert len(list(db.scalars(select(MarketDailyMetric)))) == 1
        assert report.available_on == "2026-08-31"
        assert report.roe == 0.18


def test_financial_payload_cannot_make_report_visible_before_next_trading_day(
    tmp_path: Path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'financial-visibility.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        stock = Stock(
            code="000001",
            exchange="SZSE",
            symbol="000001.SZ",
            name="平安银行",
            instrument_type="STOCK",
        )
        db.add(stock)
        db.commit()

        sync_financial_rows(
            db,
            stock,
            [
                {
                    "report_period": "2026-06-30",
                    "actual_announcement_date": "2026-07-24",
                    "available_on": "2026-07-23",
                    "roe": 0.18,
                }
            ],
            source="test-real",
            trading_days={"2026-07-24", "2026-07-27"},
        )

        report = db.scalar(select(FinancialReportSnapshot))
        assert report.available_on == "2026-07-27"


def test_daily_rows_and_etf_master_are_normalized_and_upserted(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'daily.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        stock = Stock(
            code="000001",
            exchange="SZSE",
            symbol="000001.SZ",
            name="平安银行",
            instrument_type="STOCK",
        )
        db.add(stock)
        db.commit()

        sync_daily_rows(
            db,
            stock,
            [
                {
                    "trade_date": "20260723",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "vol": 1000,
                    "amount": 2000,
                }
            ],
            source="tushare",
            amount_multiplier=1000,
            volume_multiplier=100,
        )
        sync_daily_rows(
            db,
            stock,
            [{"日期": "2026-07-23", "收盘": 10.6, "成交额": 2_100_000}],
            source="akshare",
        )
        created = sync_etf_master_rows(
            db,
            [
                {
                    "ts_code": "510300.SH",
                    "name": "沪深300ETF",
                    "list_date": "20120528",
                    "lot_size": 100,
                    "settlement_days": 1,
                }
            ],
        )

        bar = db.scalar(select(MarketDailyBar).where(MarketDailyBar.stock_id == stock.id))
        etf = db.scalar(select(Stock).where(Stock.symbol == "510300.SH"))
        assert bar.close == 10.6
        assert bar.amount == 2_100_000
        assert bar.adjusted_close is None
        assert bar.quality_status == "valid"
        assert created == 1
        assert etf.instrument_type == "ETF"
        assert etf.exchange == "SSE"
        assert etf.listing_date == "2012-05-28"

        sync_daily_rows(
            db,
            etf,
            [{"trade_date": "2026-07-23", "close": 4.05}],
            source="tushare",
        )
        etf_bar = db.scalar(
            select(MarketDailyBar).where(MarketDailyBar.stock_id == etf.id)
        )
        assert etf_bar.adjustment_factor == 1
        assert etf_bar.adjusted_close == 4.05


def test_stock_daily_refresh_invalidates_stale_adjustment_until_factor_sync(
    tmp_path: Path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'factor-refresh.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        stock = Stock(
            code="000001",
            exchange="SZSE",
            symbol="000001.SZ",
            name="平安银行",
            instrument_type="STOCK",
        )
        db.add(stock)
        db.commit()

        sync_daily_rows(
            db,
            stock,
            [{"trade_date": "2026-07-23", "close": 10}],
            source="akshare",
        )
        sync_adjustment_rows(
            db,
            stock,
            [{"trade_date": "2026-07-23", "adjustment_factor": 1.2}],
            source="tushare",
        )
        sync_daily_rows(
            db,
            stock,
            [{"trade_date": "2026-07-23", "close": 11}],
            source="akshare",
        )
        row = db.scalar(select(MarketDailyBar))
        assert row.adjusted_close is None
        assert row.adjustment_factor is None
        assert row.source == "akshare"

        sync_adjustment_rows(
            db,
            stock,
            [{"trade_date": "2026-07-23", "adjustment_factor": 1.2}],
            source="tushare",
        )
        db.refresh(row)
        assert row.adjusted_close == pytest.approx(13.2)
        assert row.source == "akshare"


def test_worker_quant_sync_includes_stock_and_etf_daily_data(tmp_path: Path):
    from app.worker import poll_quant_market_data

    database_url = f"sqlite:///{tmp_path / 'worker-sync.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            Stock(
                code="000001",
                exchange="SZSE",
                symbol="000001.SZ",
                name="平安银行",
                instrument_type="STOCK",
                status="active",
                turnover_amount=300_000_000,
            )
        )
        db.commit()

    class Source:
        name = "fake"

        def etf_master(self):
            return [{"ts_code": "510300.SH", "name": "沪深300ETF", "list_date": "20120528"}]

        def bars(self, *, symbol, timeframe, start, end):
            assert timeframe == "1d"
            return [{"trade_date": end, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100, "amount": 200_000_000}]

        def etf_bars(self, symbol, *, start, end):
            return [{"trade_date": end, "open": 4, "high": 4.1, "low": 3.9, "close": 4, "volume": 100, "amount": 200_000_000}]

        def adjustment_factors(self, symbol, *, start, end):
            return [{"trade_date": end, "adjustment_factor": 1.2}]

        def daily_metrics(self, symbol, *, start, end):
            return [{"trade_date": end, "pe_ttm": 10, "pb": 1}]

        def financial_reports(self, symbol):
            return [{"report_period": "2026-06-30", "actual_announcement_date": "2026-07-20", "roe": 0.15, "gross_margin": 0.3, "operating_cash_flow": 10, "total_assets": 100, "total_liabilities": 30}]

    result = poll_quant_market_data(
        provider=Source(),
        trading_days={"2026-07-21", "2026-07-24"},
        session_factory=lambda: Session(engine),
        current=datetime(2026, 7, 24, 16, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    with Session(engine) as db:
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        etf = db.scalar(select(Stock).where(Stock.symbol == "510300.SH"))
        assert result["stocks"] == 1
        assert result["etfs"] == 1
        assert result["daily_rows"] == 2
        assert result["errors"] == 0
        assert db.scalar(select(MarketDailyBar).where(MarketDailyBar.stock_id == stock.id)).adjustment_factor == 1.2
        assert db.scalar(select(MarketDailyBar).where(MarketDailyBar.stock_id == etf.id)).close == 4
        report = db.scalar(
            select(FinancialReportSnapshot).where(
                FinancialReportSnapshot.stock_id == stock.id
            )
        )
        assert report.available_on == "2026-07-21"


def test_worker_quant_sync_fetches_stock_payloads_concurrently(tmp_path: Path):
    from app.worker import poll_quant_market_data

    database_url = f"sqlite:///{tmp_path / 'parallel-fetch.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    symbols = ("001201.SZ", "001202.SZ")
    with Session(engine) as db:
        for index, symbol in enumerate(symbols):
            db.add(
                Stock(
                    code=symbol.split(".")[0],
                    exchange="SZSE",
                    symbol=symbol,
                    name=f"并发测试{index}",
                    instrument_type="STOCK",
                    status="active",
                    turnover_amount=300_000_000 - index,
                )
            )
        db.commit()

    barrier = Barrier(2)

    class Source:
        name = "fake"

        def etf_master(self):
            return []

        def bars(self, *, symbol, timeframe, start, end):
            assert timeframe == "1d"
            barrier.wait(timeout=2)
            return [
                {
                    "trade_date": end,
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10,
                    "volume": 100,
                    "amount": 200_000_000,
                }
            ]

        def adjustment_factors(self, symbol, *, start, end):
            return [{"trade_date": end, "adjustment_factor": 1.2}]

        def daily_metrics(self, symbol, *, start, end):
            return [{"trade_date": end, "pe_ttm": 10, "pb": 1}]

        def financial_reports(self, symbol):
            return [
                {
                    "report_period": "2026-06-30",
                    "actual_announcement_date": "2026-07-20",
                    "roe": 0.15,
                    "gross_margin": 0.3,
                    "operating_cash_flow": 10,
                    "total_assets": 100,
                    "total_liabilities": 30,
                }
            ]

    result = poll_quant_market_data(
        provider=Source(),
        trading_days={"2026-07-21", "2026-07-24"},
        session_factory=lambda: Session(engine),
        current=datetime(2026, 7, 24, 16, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
        stock_fetch_workers=2,
    )

    assert result["stocks"] == 2
    assert result["errors"] == 0
    with Session(engine) as db:
        assert db.scalar(select(MarketDailyBar).where(MarketDailyBar.adjusted_close.is_(None))) is None


def test_quant_sync_uses_mootdx_only_for_stock_daily_and_adjustment(tmp_path: Path):
    from app.worker import poll_quant_market_data

    database_url = f"sqlite:///{tmp_path / 'split-provider.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            Stock(
                code="000001",
                exchange="SZSE",
                symbol="000001.SZ",
                name="平安银行",
                instrument_type="STOCK",
                status="active",
                turnover_amount=300_000_000,
            )
        )
        db.commit()

    calls = []

    class PublicSource:
        name = "akshare"
        capabilities = frozenset(
            {"daily", "adjustment", "daily_metric", "financial", "etf_master"}
        )

        def health(self):
            return True, None

        def etf_master(self):
            return []

        def bars(self, **_kwargs):
            raise AssertionError("股票日线应由 mootdx 提供")

        def adjustment_factors(self, *_args, **_kwargs):
            raise AssertionError("股票复权应由 mootdx 提供")

        def daily_metrics(self, symbol, *, start, end):
            calls.append(("metrics", symbol))
            return [{"trade_date": end, "pe_ttm": 10, "pb": 1}]

        def financial_reports(self, symbol):
            calls.append(("financial", symbol))
            return [
                {
                    "report_period": "2026-06-30",
                    "actual_announcement_date": "2026-07-20",
                    "roe": 0.15,
                    "gross_margin": 0.3,
                    "operating_cash_flow": 10,
                    "total_assets": 100,
                    "total_liabilities": 30,
                }
            ]

    class TdxSource:
        name = "mootdx"
        capabilities = frozenset({"daily", "adjustment"})

        def health(self):
            return True, None

        def bars(self, *, symbol, timeframe, start, end):
            calls.append(("daily", symbol))
            assert timeframe == "1d"
            return [
                {
                    "trade_date": end,
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10,
                    "volume": 100,
                    "amount": 200_000_000,
                }
            ]

        def adjustment_factors(self, symbol, *, start, end):
            calls.append(("adjustment", symbol))
            return [{"trade_date": end, "adjustment_factor": 1.2}]

    result = poll_quant_market_data(
        router=ProviderRouter([PublicSource(), TdxSource()]),
        trading_days={"2026-07-21", "2026-07-24"},
        session_factory=lambda: Session(engine),
        current=datetime(2026, 7, 24, 16, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
        stock_fetch_workers=1,
    )

    assert result["stocks"] == 1
    assert calls == [
        ("daily", "000001.SZ"),
        ("adjustment", "000001.SZ"),
        ("metrics", "000001.SZ"),
        ("financial", "000001.SZ"),
    ]
    with Session(engine) as db:
        bar = db.scalar(select(MarketDailyBar))
        metric = db.scalar(select(MarketDailyMetric))
        report = db.scalar(select(FinancialReportSnapshot))
        assert bar.source == "mootdx"
        assert bar.adjustment_factor == 1.2
        assert metric.source == "akshare"
        assert report.source == "akshare"


def test_quant_sync_stock_universe_uses_liquidity_but_always_keeps_holdings(
    tmp_path: Path,
):
    database_url = f"sqlite:///{tmp_path / 'sync-universe.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(
        database_url=database_url,
        live_enabled=False,
        broker_adapter="simulation",
    )
    with Session(engine) as db:
        seed_database(db, settings)
        configs = seed_quant_strategy_runtimes(db, settings)
        config = configs["multi_factor_core"]
        account = config.simulation_account_id
        for existing in db.scalars(select(Stock)):
            existing.status = "inactive"
        rows = []
        for index, turnover in enumerate((300_000_000, 200_000_000, 1_000_000), start=1):
            stock = Stock(
                code=f"001{index:03d}",
                exchange="SZSE",
                symbol=f"001{index:03d}.SZ",
                name=f"测试股票{index}",
                status="active",
                instrument_type="STOCK",
                turnover_amount=turnover,
            )
            db.add(stock)
            db.flush()
            rows.append(stock)
        db.add(
            Position(
                account_id=account,
                mode="SIMULATION",
                stock_id=rows[-1].id,
                quantity=100,
                available_quantity=100,
                average_cost=10,
                market_value=1_000,
            )
        )
        db.commit()

        selected = quant_sync_stock_universe(db, limit=2)

        assert [stock.symbol for stock in selected[:2]] == [
            "001001.SZ",
            "001002.SZ",
        ]
        assert rows[-1].symbol in {stock.symbol for stock in selected}
        assert len(selected) == 3


def test_quant_sync_stock_universe_never_ranks_unknown_or_partial_liquidity(
    tmp_path: Path,
):
    database_url = f"sqlite:///{tmp_path / 'sync-universe-readiness.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(
        database_url=database_url,
        live_enabled=False,
        broker_adapter="simulation",
    )
    with Session(engine) as db:
        seed_database(db, settings)
        configs = seed_quant_strategy_runtimes(db, settings)
        account_id = configs["multi_factor_core"].simulation_account_id
        for existing in db.scalars(select(Stock)):
            existing.status = "inactive"

        complete = Stock(
            code="001101",
            exchange="SZSE",
            symbol="001101.SZ",
            name="完整历史",
            status="active",
            instrument_type="STOCK",
            turnover_amount=50_000_000,
        )
        partial = Stock(
            code="001102",
            exchange="SZSE",
            symbol="001102.SZ",
            name="部分历史",
            status="active",
            instrument_type="STOCK",
            turnover_amount=100_000_000,
        )
        unknown = Stock(
            code="001103",
            exchange="SZSE",
            symbol="001103.SZ",
            name="未知流动性",
            status="active",
            instrument_type="STOCK",
            turnover_amount=None,
        )
        held_unknown = Stock(
            code="001104",
            exchange="SZSE",
            symbol="001104.SZ",
            name="持仓未知流动性",
            status="active",
            instrument_type="STOCK",
            turnover_amount=None,
        )
        db.add_all([complete, partial, unknown, held_unknown])
        db.flush()
        for index in range(20):
            db.add(
                MarketDailyBar(
                    stock_id=complete.id,
                    trade_date=f"2026-06-{index + 1:02d}",
                    open=10,
                    high=10,
                    low=10,
                    close=10,
                    volume=100,
                    amount=200_000_000,
                    quality_status="valid",
                    source="test",
                )
            )
        db.add(
            MarketDailyBar(
                stock_id=partial.id,
                trade_date="2026-06-20",
                open=10,
                high=10,
                low=10,
                close=10,
                volume=100,
                amount=900_000_000,
                quality_status="valid",
                source="test",
            )
        )
        db.add(
            Position(
                account_id=account_id,
                mode="SIMULATION",
                stock_id=held_unknown.id,
                quantity=100,
                available_quantity=100,
                average_cost=10,
                market_value=1_000,
            )
        )
        db.commit()

        selected = quant_sync_stock_universe(db, limit=3)

        assert [stock.symbol for stock in selected] == [
            "001101.SZ",
            "001102.SZ",
            "001104.SZ",
        ]


def test_configured_etf_symbols_follow_both_etf_strategy_configs(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'etf-pool.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(
        database_url=database_url,
        live_enabled=False,
        broker_adapter="simulation",
    )
    with Session(engine) as db:
        seed_database(db, settings)
        configs = seed_quant_strategy_runtimes(db, settings)
        first = [
            "510050.SH",
            "510500.SH",
            "159915.SZ",
            "510880.SH",
            "511010.SH",
            "518880.SH",
        ]
        second = [
            "510300.SH",
            "512100.SH",
            "159949.SZ",
            "512890.SH",
            "511260.SH",
            "159934.SZ",
        ]
        configs["regime_allocator"].parameters = {
            **configs["regime_allocator"].parameters,
            "etf_universe": first,
        }
        configs["risk_parity_overlay"].parameters = {
            **configs["risk_parity_overlay"].parameters,
            "etf_universe": second,
        }
        db.commit()

        assert configured_etf_symbols(db) == tuple(sorted(set(first + second)))


def test_quant_worker_prioritizes_tushare_then_falls_back_for_daily_and_etf(
    tmp_path: Path,
):
    from app.worker import poll_quant_market_data

    database_url = f"sqlite:///{tmp_path / 'fallback-sync.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(
        database_url=database_url,
        live_enabled=False,
        broker_adapter="simulation",
    )
    with Session(engine) as db:
        seed_database(db, settings)
        seed_quant_strategy_runtimes(db, settings)
        for stock in db.scalars(select(Stock).where(Stock.instrument_type == "STOCK")):
            stock.status = "inactive"
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        stock.status = "active"
        stock.listing_date = "1991-04-03"
        stock.turnover_amount = 300_000_000
        db.commit()

    calls = []

    class Primary:
        name = "tushare"
        capabilities = frozenset(
            {
                "daily",
                "adjustment",
                "daily_metric",
                "financial",
                "etf_master",
                "etf_daily",
            }
        )

        def health(self):
            return True, None

        def bars(self, **kwargs):
            calls.append(("primary_daily", kwargs))
            raise RuntimeError("Tushare 日线暂不可用")

        def etf_master(self):
            calls.append(("primary_etf_master", {}))
            raise RuntimeError("Tushare ETF 主数据暂不可用")

        def etf_bars(self, **kwargs):
            calls.append(("primary_etf_daily", kwargs))
            raise RuntimeError("Tushare ETF 日线暂不可用")

        def adjustment_factors(self, symbol, *, start, end):
            return [{"trade_date": end, "adjustment_factor": 1.2}]

        def daily_metrics(self, symbol, *, start, end):
            return [{"trade_date": end, "pe_ttm": 10, "pb": 1}]

        def financial_reports(self, symbol):
            return [
                {
                    "report_period": "2026-06-30",
                    "actual_announcement_date": "2026-07-20",
                    "roe": 0.15,
                    "gross_margin": 0.30,
                    "operating_cash_flow": 10,
                    "total_assets": 100,
                    "total_liabilities": 30,
                }
            ]

    class Fallback:
        name = "akshare"
        capabilities = frozenset(
            {"daily", "adjustment", "etf_master", "etf_daily"}
        )

        def health(self):
            return True, None

        def bars(self, **kwargs):
            calls.append(("fallback_daily", kwargs))
            return [
                {
                    "trade_date": kwargs["end"],
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10,
                    "volume": 100,
                    "amount": 200_000_000,
                }
            ]

        def adjustment_factors(self, symbol, *, start, end):
            calls.append(("fallback_adjustment", {"symbol": symbol}))
            return [{"trade_date": end, "adjustment_factor": 9.9}]

        def etf_master(self):
            calls.append(("fallback_etf_master", {}))
            return [
                {"ts_code": "510300.SH", "name": "沪深300ETF"},
                {"ts_code": "512000.SH", "name": "非配置ETF"},
            ]

        def etf_bars(self, **kwargs):
            calls.append(("fallback_etf_daily", kwargs))
            return [
                {
                    "trade_date": kwargs["end"],
                    "open": 4,
                    "high": 4.1,
                    "low": 3.9,
                    "close": 4,
                    "volume": 100,
                    "amount": 200_000_000,
                }
            ]

    current = datetime(2026, 7, 24, 16, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = poll_quant_market_data(
        router=ProviderRouter([Fallback(), Primary()]),
        session_factory=lambda: Session(engine),
        current=current,
    )

    assert any(name == "primary_daily" for name, _kwargs in calls)
    assert any(name == "primary_etf_master" for name, _kwargs in calls)
    assert any(name == "primary_etf_daily" for name, _kwargs in calls)
    fallback_daily = [kwargs for name, kwargs in calls if name == "fallback_daily"]
    etf_daily = [kwargs for name, kwargs in calls if name == "fallback_etf_daily"]
    assert fallback_daily
    assert min(kwargs["start"] for kwargs in fallback_daily) <= "2023-04-11"
    assert {kwargs["symbol"] for kwargs in etf_daily} == {"510300.SH"}
    with Session(engine) as db:
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        bar = db.scalar(
            select(MarketDailyBar).where(MarketDailyBar.stock_id == stock.id)
        )
        assert result["errors"] == 0
        assert bar.source == "akshare"
        assert bar.adjustment_factor == 1.2
        assert not any(name == "fallback_adjustment" for name, _kwargs in calls)
        assert db.scalar(select(Stock.id).where(Stock.symbol == "512000.SH")) is None


def test_quant_worker_keeps_etf_sync_independent_when_stock_adjustment_is_missing(
    tmp_path: Path,
):
    from app.worker import poll_quant_market_data

    database_url = f"sqlite:///{tmp_path / 'etf-independent.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(
        database_url=database_url,
        live_enabled=False,
        broker_adapter="simulation",
    )
    with Session(engine) as db:
        seed_database(db, settings)
        seed_quant_strategy_runtimes(db, settings)
        for stock in db.scalars(select(Stock).where(Stock.instrument_type == "STOCK")):
            stock.status = "inactive"
        db.commit()

    class PublicSource:
        name = "akshare"
        capabilities = frozenset({"daily", "etf_master", "etf_daily"})

        def health(self):
            return True, None

        def etf_master(self):
            return [{"ts_code": "510300.SH", "name": "沪深300ETF"}]

        def etf_bars(self, symbol, *, start, end):
            return [
                {
                    "trade_date": end,
                    "open": 4,
                    "high": 4.1,
                    "low": 3.9,
                    "close": 4,
                    "volume": 100,
                    "amount": 200_000_000,
                }
            ]

        def bars(self, **kwargs):
            return [
                {
                    "trade_date": kwargs["end"],
                    "open": 4600,
                    "high": 4700,
                    "low": 4550,
                    "close": 4650,
                    "volume": 100,
                    "amount": 200_000_000,
                }
            ]

    current = datetime(2026, 7, 24, 16, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = poll_quant_market_data(
        router=ProviderRouter([PublicSource()]),
        trading_days={"2026-07-24", "2026-07-27"},
        session_factory=lambda: Session(engine),
        current=current,
    )

    with Session(engine) as db:
        state = db.scalar(
            select(DataSourceState).where(
                DataSourceState.provider == "quant_etf_daily"
            )
        )
        assert result["etfs"] == 1
        assert state.healthy is True


def test_quant_worker_uses_one_cross_section_request_per_mature_daily_dataset(
    tmp_path: Path,
):
    from app.worker import poll_quant_market_data

    database_url = f"sqlite:///{tmp_path / 'batch-incremental.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(
        database_url=database_url,
        live_enabled=False,
        broker_adapter="simulation",
    )
    symbols = ["001201.SZ", "001202.SZ"]
    with Session(engine) as db:
        seed_database(db, settings)
        seed_quant_strategy_runtimes(db, settings)
        for stock in db.scalars(select(Stock)):
            stock.status = "inactive"
        for stock_index, symbol in enumerate(symbols):
            stock = Stock(
                code=symbol.split(".")[0],
                exchange="SZSE",
                symbol=symbol,
                name=f"批量股票{stock_index}",
                status="active",
                instrument_type="STOCK",
                listing_date="2010-01-01",
                turnover_amount=300_000_000 - stock_index,
            )
            db.add(stock)
            db.flush()
            for offset in range(520):
                day = datetime(2025, 1, 1) + timedelta(days=offset)
                db.add(
                    MarketDailyBar(
                        stock_id=stock.id,
                        trade_date=day.date().isoformat(),
                        open=10,
                        high=11,
                        low=9,
                        close=10,
                        adjusted_close=10,
                        adjustment_factor=1,
                        volume=100,
                        amount=200_000_000,
                        source="tushare",
                    )
                )
            db.add(
                FinancialReportSnapshot(
                    stock_id=stock.id,
                    report_period="2026-03-31",
                    announcement_date="2026-04-20",
                    actual_announcement_date="2026-04-20",
                    available_on="2026-04-21",
                    roe=0.15,
                    gross_margin=0.3,
                    operating_cash_flow=10,
                    total_assets=100,
                    total_liabilities=30,
                    source="tushare",
                )
            )
        db.commit()

    calls = []

    class Source:
        name = "tushare"

        def etf_master(self):
            return []

        def daily_cross_section(self, trade_date):
            calls.append(("daily_cross_section", trade_date))
            return [
                {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10,
                    "volume": 100,
                    "amount": 200_000_000,
                }
                for symbol in symbols
            ]

        def adjustment_cross_section(self, trade_date):
            calls.append(("adjustment_cross_section", trade_date))
            return [
                {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "adjustment_factor": 1.2,
                }
                for symbol in symbols
            ]

        def daily_metric_cross_section(self, trade_date):
            calls.append(("daily_metric_cross_section", trade_date))
            return [
                {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "pe_ttm": 10,
                    "pb": 1,
                }
                for symbol in symbols
            ]

        def financial_report_cross_sections(self, periods):
            calls.append(("financial_report_cross_sections", tuple(periods)))
            return [
                {
                    "symbol": symbol,
                    "report_period": "2026-06-30",
                    "actual_announcement_date": "2026-07-20",
                    "roe": 0.15,
                    "gross_margin": 0.3,
                    "operating_cash_flow": 10,
                    "total_assets": 100,
                    "total_liabilities": 30,
                }
                for symbol in symbols
            ]

        def bars(self, *, symbol, timeframe, start, end):
            assert symbol == "000300.SH"
            return [
                {
                    "trade_date": end,
                    "open": 4000,
                    "high": 4100,
                    "low": 3900,
                    "close": 4050,
                    "volume": 100,
                    "amount": 200_000_000,
                }
            ]

        def adjustment_factors(self, symbol, *, start, end):
            raise AssertionError(f"成熟股票不应逐股拉复权: {symbol}")

        def daily_metrics(self, symbol, *, start, end):
            raise AssertionError(f"成熟股票不应逐股拉估值: {symbol}")

        def financial_reports(self, symbol):
            raise AssertionError(f"支持横截面时不应逐股拉财务: {symbol}")

    current = datetime(2026, 7, 24, 16, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    poll_quant_market_data(
        provider=Source(),
        trading_days={"2026-07-24", "2026-07-27"},
        session_factory=lambda: Session(engine),
        current=current,
    )

    assert calls == [
        ("daily_cross_section", "2026-07-24"),
        ("adjustment_cross_section", "2026-07-24"),
        ("daily_metric_cross_section", "2026-07-24"),
        (
            "financial_report_cross_sections",
            (
                "2026-06-30",
                "2026-03-31",
                "2025-12-31",
                "2025-09-30",
            ),
        ),
    ]
    with Session(engine) as db:
        for symbol in symbols:
            stock = db.scalar(select(Stock).where(Stock.symbol == symbol))
            bar = db.scalar(
                select(MarketDailyBar).where(
                    MarketDailyBar.stock_id == stock.id,
                    MarketDailyBar.trade_date == "2026-07-24",
                )
            )
            metric = db.scalar(
                select(MarketDailyMetric).where(
                    MarketDailyMetric.stock_id == stock.id,
                    MarketDailyMetric.trade_date == "2026-07-24",
                )
            )
            assert bar.adjustment_factor == 1.2
            assert metric.pe_ttm == 10


def test_quant_production_sync_fails_closed_when_exchange_calendar_is_unavailable(
    tmp_path: Path,
    monkeypatch,
):
    from app.worker import poll_quant_market_data

    database_url = f"sqlite:///{tmp_path / 'calendar-closed.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)

    class Primary:
        name = "tushare"
        capabilities = frozenset({"adjustment"})

        def health(self):
            return True, None

    class BrokenCalendar:
        def trading_days(self, *, start, end):
            raise RuntimeError("交易所日历暂不可用")

    monkeypatch.setattr(
        "app.worker.market_router",
        lambda: ProviderRouter([Primary()]),
    )
    monkeypatch.setattr(
        "app.worker.trading_calendar_service",
        lambda: BrokenCalendar(),
    )

    result = poll_quant_market_data(
        session_factory=lambda: Session(engine),
        current=datetime(2026, 7, 24, 16, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result["stocks"] == 0
    assert result["errors"] == 1
    assert "交易所日历" in result["message"]


def test_quant_sync_tracks_financial_failure_without_invalidating_stock_daily(
    tmp_path: Path,
):
    from app.worker import poll_quant_market_data

    database_url = f"sqlite:///{tmp_path / 'dataset-state.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        stock = Stock(
            code="000001",
            exchange="SZSE",
            symbol="000001.SZ",
            name="平安银行",
            instrument_type="STOCK",
            status="active",
            turnover_amount=300_000_000,
        )
        db.add(stock)
        db.commit()

    class Source:
        name = "tushare"

        def etf_master(self):
            return []

        def bars(self, *, symbol, timeframe, start, end):
            return [{
                "trade_date": end,
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
                "volume": 100,
                "amount": 200_000_000,
            }]

        def adjustment_factors(self, symbol, *, start, end):
            return [{"trade_date": end, "adjustment_factor": 1.2}]

        def daily_metrics(self, symbol, *, start, end):
            return [{"trade_date": end, "pe_ttm": 10, "pb": 1}]

        def financial_reports(self, symbol):
            raise RuntimeError("财务权限不足")

    current = datetime(2026, 7, 24, 16, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = poll_quant_market_data(
        provider=Source(),
        trading_days={"2026-07-24", "2026-07-27"},
        session_factory=lambda: Session(engine),
        current=current,
    )

    with Session(engine) as db:
        states = {
            row.provider: row
            for row in db.scalars(
                select(DataSourceState).where(
                    DataSourceState.provider.in_([
                        "quant_stock_daily",
                        "quant_daily_metric",
                        "quant_financial",
                    ])
                )
            )
        }
        assert result["errors"] == 1
        assert states["quant_stock_daily"].healthy is True
        assert states["quant_daily_metric"].healthy is True
        assert states["quant_financial"].healthy is False
        assert "财务权限不足" in states["quant_financial"].last_error


def test_quant_dataset_batch_accepts_ninety_eight_percent_completeness(
    tmp_path: Path,
):
    from app.worker import poll_quant_market_data

    database_url = f"sqlite:///{tmp_path / 'dataset-completeness.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        for index in range(50):
            db.add(
                Stock(
                    code=f"002{index:03d}",
                    exchange="SZSE",
                    symbol=f"002{index:03d}.SZ",
                    name=f"完整率测试{index}",
                    instrument_type="STOCK",
                    status="active",
                    turnover_amount=300_000_000 - index,
                )
            )
        db.commit()

    class Source:
        name = "tushare"

        def etf_master(self):
            return []

        def bars(self, *, symbol, timeframe, start, end):
            return [{
                "trade_date": end,
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
                "volume": 100,
                "amount": 200_000_000,
            }]

        def adjustment_factors(self, symbol, *, start, end):
            return [{"trade_date": end, "adjustment_factor": 1.2}]

        def daily_metrics(self, symbol, *, start, end):
            return [{"trade_date": end, "pe_ttm": 10, "pb": 1}]

        def financial_reports(self, symbol):
            if symbol == "002000.SZ":
                raise RuntimeError("单股财务缺失")
            return [{
                "report_period": "2026-06-30",
                "actual_announcement_date": "2026-07-20",
                "roe": 0.15,
                "gross_margin": 0.30,
                "operating_cash_flow": 10,
                "total_assets": 100,
                "total_liabilities": 30,
            }]

    current = datetime(2026, 7, 24, 16, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    poll_quant_market_data(
        provider=Source(),
        trading_days={"2026-07-24", "2026-07-27"},
        session_factory=lambda: Session(engine),
        current=current,
    )

    with Session(engine) as db:
        financial = db.scalar(
            select(DataSourceState).where(
                DataSourceState.provider == "quant_financial"
            )
        )
        assert financial.healthy is True
        assert financial.last_error is None


def test_quant_sync_does_not_mark_empty_current_payloads_healthy(
    tmp_path: Path,
):
    from app.worker import poll_quant_market_data

    database_url = f"sqlite:///{tmp_path / 'empty-payload.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            Stock(
                code="000001",
                exchange="SZSE",
                symbol="000001.SZ",
                name="平安银行",
                instrument_type="STOCK",
                status="active",
                turnover_amount=300_000_000,
            )
        )
        db.commit()

    class EmptySource:
        name = "tushare"

        def etf_master(self):
            return []

        def bars(self, *, symbol, timeframe, start, end):
            return []

        def adjustment_factors(self, symbol, *, start, end):
            return []

        def daily_metrics(self, symbol, *, start, end):
            return []

        def financial_reports(self, symbol):
            return []

    current = datetime(2026, 7, 24, 16, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = poll_quant_market_data(
        provider=EmptySource(),
        trading_days={"2026-07-24", "2026-07-27"},
        session_factory=lambda: Session(engine),
        current=current,
    )

    with Session(engine) as db:
        states = {
            state.provider: state
            for state in db.scalars(
                select(DataSourceState).where(
                    DataSourceState.provider.in_([
                        "quant_stock_daily",
                        "quant_daily_metric",
                        "quant_financial",
                    ])
                )
            )
        }
        assert result["errors"] >= 3
        assert states["quant_stock_daily"].healthy is False
        assert states["quant_daily_metric"].healthy is False
        assert states["quant_financial"].healthy is False


def test_quant_sync_does_not_mark_empty_etf_or_benchmark_payloads_healthy(
    tmp_path: Path,
):
    from app.worker import poll_quant_market_data

    database_url = f"sqlite:///{tmp_path / 'empty-etf-benchmark.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(
        database_url=database_url,
        live_enabled=False,
        broker_adapter="simulation",
    )
    with Session(engine) as db:
        seed_database(db, settings)
        seed_quant_strategy_runtimes(db, settings)
        for stock in db.scalars(
            select(Stock).where(Stock.instrument_type == "STOCK")
        ):
            stock.status = "inactive"
        db.commit()

    class EmptyMarketSource:
        name = "tushare"

        def etf_master(self):
            return [{"ts_code": "510300.SH", "name": "沪深300ETF"}]

        def bars(self, **_kwargs):
            return []

        def etf_bars(self, *_args, **_kwargs):
            return []

    current = datetime(2026, 7, 24, 16, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = poll_quant_market_data(
        provider=EmptyMarketSource(),
        trading_days={"2026-07-24", "2026-07-27"},
        session_factory=lambda: Session(engine),
        current=current,
    )

    with Session(engine) as db:
        states = {
            state.provider: state
            for state in db.scalars(
                select(DataSourceState).where(
                    DataSourceState.provider.in_([
                        "quant_etf_daily",
                        "quant_benchmark_daily",
                    ])
                )
            )
        }
        assert result["errors"] >= 2
        assert states["quant_etf_daily"].healthy is False
        assert states["quant_benchmark_daily"].healthy is False


def test_quant_data_failure_notification_is_deduplicated_per_day_and_dataset(
    tmp_path: Path,
):
    from app.worker import poll_quant_market_data

    database_url = f"sqlite:///{tmp_path / 'data-notify.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            Stock(
                code="000001",
                exchange="SZSE",
                symbol="000001.SZ",
                name="平安银行",
                instrument_type="STOCK",
                status="active",
                turnover_amount=300_000_000,
            )
        )
        db.add(
            NotificationChannel(
                type="email",
                name="数据告警",
                enabled=True,
                recipient="ops@example.com",
                secret_ref="SMTP_PASSWORD",
                event_types=["quant_strategy_data_failed"],
            )
        )
        db.commit()

    class EmptySource:
        name = "tushare"

        def etf_master(self):
            return []

        def bars(self, **_kwargs):
            return []

        def adjustment_factors(self, *args, **kwargs):
            return []

        def daily_metrics(self, *args, **kwargs):
            return []

        def financial_reports(self, _symbol):
            return []

    current = datetime(2026, 7, 24, 16, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    for _ in range(2):
        poll_quant_market_data(
            provider=EmptySource(),
            trading_days={"2026-07-24", "2026-07-27"},
            session_factory=lambda: Session(engine),
            current=current,
        )

    with Session(engine) as db:
        deliveries = list(
            db.scalars(
                select(NotificationDelivery).where(
                    NotificationDelivery.event_type
                    == "quant_strategy_data_failed"
                )
            )
        )
        dataset_keys = [item.payload["dataset"] for item in deliveries]
        assert len(dataset_keys) == len(set(dataset_keys))
        assert "quant_stock_daily" in dataset_keys
        assert "quant_daily_metric" in dataset_keys
        assert "quant_financial" in dataset_keys
