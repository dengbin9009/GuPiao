from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..data_sync import refresh_quotes
from ..models import MarketDailyBar, Stock, StrategyConfig
from ..trading_agents.market_snapshot import (
    _upsert_daily_rows,
    latest_completed_daily_date,
)
from .config import PROBABILITY_PORTFOLIO_DEFAULTS


SNAPSHOT_WORKERS = 8


@dataclass(frozen=True)
class IntradayFactors:
    vwap: float
    tail_30m_return: float
    last_completed_at: datetime


def _timestamp(value: Any, current: datetime) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=current.tzinfo)
    return result.astimezone(current.tzinfo)


def calculate_intraday_factors(
    rows: list[dict[str, Any]],
    *,
    current: datetime,
) -> IntradayFactors:
    cutoff = current.replace(second=0, microsecond=0)
    completed = []
    for row in rows:
        timestamp = _timestamp(row.get("timestamp"), current)
        if timestamp is None or timestamp.date() != current.date() or timestamp >= cutoff:
            continue
        close = float(row.get("close", 0) or 0)
        volume = float(row.get("volume", 0) or 0)
        amount = float(row.get("amount", 0) or 0)
        if close <= 0:
            continue
        completed.append((timestamp, close, volume, amount))
    completed.sort(key=lambda item: item[0])
    if len(completed) < 30:
        raise ValueError("截至决策时点的已完成分钟线不足30根")
    volume_sum = sum(item[2] for item in completed)
    amount_sum = sum(item[3] for item in completed)
    if volume_sum <= 0 or amount_sum <= 0:
        raise ValueError("已完成分钟线缺少真实成交量或成交额")
    return IntradayFactors(
        vwap=amount_sum / volume_sum,
        tail_30m_return=completed[-1][1] / completed[-30][1] - 1,
        last_completed_at=completed[-1][0],
    )


def _benchmark_stock(db: Session) -> Stock:
    benchmark = db.scalar(select(Stock).where(Stock.symbol == "000300.SH"))
    if benchmark is None:
        benchmark = Stock(
            code="000300",
            exchange="SSE",
            symbol="000300.SH",
            name="沪深300",
            status="benchmark",
        )
        db.add(benchmark)
        db.flush()
    return benchmark


def sync_probability_market_data(
    db: Session,
    config: StrategyConfig,
    router: Any,
    *,
    current: datetime,
) -> dict[str, int]:
    if config.mode != "SIMULATION" or not config.simulation_account_id:
        raise ValueError("概率组合行情固化仅支持绑定模拟账户的策略")
    parameters = {**PROBABILITY_PORTFOLIO_DEFAULTS, **(config.parameters or {})}
    stocks = list(
        db.scalars(
            select(Stock)
            .where(Stock.status == "active", Stock.exchange.in_(["SSE", "SZSE"]))
            .order_by(Stock.symbol)
        )
    )
    symbols = [stock.symbol for stock in stocks]
    routed = router.call("realtime", "quotes", symbols=symbols)
    quote_provider = type(
        "ProbabilitySnapshotQuotes",
        (),
        {
            "name": routed.provider,
            "quotes": lambda self, _symbols: list(routed.data),
        },
    )()
    quote_result = refresh_quotes(db, quote_provider, symbols)
    db.expire_all()

    prefilter_size = max(1, min(100, int(parameters.get("prefilter_size", 100))))
    selected = list(
        db.scalars(
            select(Stock)
            .where(
                Stock.status == "active",
                Stock.exchange.in_(["SSE", "SZSE"]),
                Stock.turnover_amount.is_not(None),
            )
            .order_by(func.coalesce(Stock.turnover_amount, 0).desc(), Stock.symbol)
            .limit(prefilter_size)
        )
    )
    benchmark = _benchmark_stock(db)
    latest_completed = latest_completed_daily_date(current)
    start = (current.date() - timedelta(days=140)).isoformat()
    daily_targets = [*selected, benchmark]

    def needs_daily_refresh(stock: Stock) -> bool:
        bars = list(
            db.scalars(
                select(MarketDailyBar)
                .where(
                    MarketDailyBar.stock_id == stock.id,
                    MarketDailyBar.trade_date <= latest_completed,
                )
                .order_by(MarketDailyBar.trade_date.desc())
                .limit(20)
            )
        )
        return len(bars) < 20 or bars[0].trade_date < latest_completed

    def fetch_daily(stock: Stock):
        return router.call(
            "daily",
            "bars",
            symbol=stock.symbol,
            timeframe="1d",
            start=start,
            end=latest_completed,
        )

    def fetch_factors(stock: Stock):
        finance = None
        if not stock.float_shares or not stock.listing_date:
            finance = router.call("finance", "finance", symbol=stock.symbol)
        minute = router.call(
            "minute",
            "bars",
            symbol=stock.symbol,
            timeframe="1m",
            start=current.date().isoformat(),
            end=current.date().isoformat(),
        )
        return finance, calculate_intraday_factors(list(minute.data), current=current)

    candidate_errors = 0
    daily_rows = 0
    daily_refresh_targets = [stock for stock in daily_targets if needs_daily_refresh(stock)]
    with (
        ThreadPoolExecutor(max_workers=SNAPSHOT_WORKERS) as factor_executor,
        ThreadPoolExecutor(max_workers=SNAPSHOT_WORKERS) as daily_executor,
    ):
        daily_futures = {
            daily_executor.submit(fetch_daily, stock): stock
            for stock in daily_refresh_targets
        }
        factor_futures = {
            factor_executor.submit(fetch_factors, stock): stock for stock in selected
        }
        for future in as_completed(daily_futures):
            stock = daily_futures[future]
            try:
                daily = future.result()
                daily_rows += _upsert_daily_rows(
                    db,
                    stock=stock,
                    rows=list(daily.data),
                    provider=daily.provider,
                    latest_completed_date=latest_completed,
                )
            except Exception:
                candidate_errors += 1
        for future in as_completed(factor_futures):
            stock = factor_futures[future]
            try:
                finance, factors = future.result()
                finance_data = finance.data if finance is not None else {}
                float_shares = float(
                    finance_data.get("float_shares", stock.float_shares or 0) or 0
                )
                listing_date = finance_data.get("listing_date", stock.listing_date)
                if float_shares <= 0 or not listing_date:
                    raise ValueError("流通股本或上市日期缺失")
                stock.float_shares = float_shares
                stock.listing_date = str(listing_date)
                stock.turnover_rate = (
                    float(stock.volume or 0) / float_shares
                    if stock.volume
                    else None
                )
                stock.vwap = factors.vwap
                stock.tail_30m_return = factors.tail_30m_return
                stock.factor_updated_at = current
                finance_provider = finance.provider if finance is not None else "finance-cache"
                stock.quote_source = f"{stock.quote_source}+{finance_provider}+minute"
            except Exception:
                stock.factor_updated_at = None
                candidate_errors += 1
    db.commit()
    return {
        "quote_updated": quote_result.updated,
        "quote_missing": len(quote_result.missing),
        "minute_symbols": len(selected),
        "daily_rows": daily_rows,
        "candidate_errors": candidate_errors,
        "errors": 0,
    }
