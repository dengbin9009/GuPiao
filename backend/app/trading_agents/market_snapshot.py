from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data_sync import refresh_quotes
from ..models import MarketDailyBar, Position, Stock, StrategyConfig, now
from .config import TRADING_AGENTS_DEFAULTS


def _field(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in {None, ""}:
            return value
    return default


def _daily_date(row: dict[str, Any]) -> str:
    value = _field(row, "trade_date", "date", "日期", default="")
    text = str(value).replace("/", "-")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10]


def _upsert_daily_rows(
    db: Session,
    *,
    stock: Stock,
    rows: list[dict[str, Any]],
    provider: str,
    latest_completed_date: str,
) -> int:
    changed = 0
    for row in rows:
        trade_date = _daily_date(row)
        if not trade_date or trade_date > latest_completed_date:
            continue
        close = float(_field(row, "close", "收盘", default=0) or 0)
        if close <= 0:
            continue
        bar = db.scalar(
            select(MarketDailyBar).where(
                MarketDailyBar.stock_id == stock.id,
                MarketDailyBar.trade_date == trade_date,
            )
        )
        if bar is None:
            bar = MarketDailyBar(stock_id=stock.id, trade_date=trade_date)
            db.add(bar)
        bar.open = float(_field(row, "open", "开盘", default=close) or close)
        bar.high = float(_field(row, "high", "最高", default=close) or close)
        bar.low = float(_field(row, "low", "最低", default=close) or close)
        bar.close = close
        bar.volume = float(_field(row, "volume", "vol", "成交量", default=0) or 0)
        amount = float(_field(row, "amount", "成交额", default=0) or 0)
        bar.amount = amount * 1000 if provider == "tushare" else amount
        bar.source = provider
        bar.captured_at = now()
        changed += 1
    return changed


def sync_agent_market_data(
    db: Session,
    config: StrategyConfig,
    router: Any,
    *,
    current: datetime | None = None,
) -> dict[str, int]:
    current = current or now()
    if config.mode != "SIMULATION" or not config.simulation_account_id:
        raise ValueError("TradingAgents 行情固化仅支持绑定模拟账户的策略")
    parameters = {**TRADING_AGENTS_DEFAULTS, **(config.parameters or {})}
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
        "AgentSnapshotQuotes",
        (),
        {
            "name": routed.provider,
            "quotes": lambda self, _symbols: list(routed.data),
        },
    )()
    quote_result = refresh_quotes(db, quote_provider, symbols)
    db.expire_all()

    holding_ids = set(
        db.scalars(
            select(Position.stock_id).where(
                Position.account_id == config.simulation_account_id,
                Position.mode == "SIMULATION",
                Position.quantity > 0,
            )
        )
    )
    ranked = sorted(
        stocks,
        key=lambda stock: (
            -float(stock.turnover_amount or 0),
            stock.symbol,
        ),
    )[: int(parameters["prefilter_size"])]
    selected_by_id = {stock.id: stock for stock in ranked}
    for stock in db.scalars(select(Stock).where(Stock.id.in_(holding_ids))):
        selected_by_id[stock.id] = stock

    latest_completed = (current.date() - timedelta(days=1)).isoformat()
    start = (current.date() - timedelta(days=140)).isoformat()
    changed = 0
    errors = 0
    selected_stocks = sorted(selected_by_id.values(), key=lambda item: item.symbol)

    def fetch_daily(symbol: str):
        return router.call(
            "daily",
            "bars",
            symbol=symbol,
            timeframe="1d",
            start=start,
            end=latest_completed,
        )

    fetched: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(selected_stocks)))) as executor:
        futures = {
            executor.submit(fetch_daily, stock.symbol): stock.symbol
            for stock in selected_stocks
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                fetched[symbol] = future.result()
            except Exception:
                errors += 1

    for stock in selected_stocks:
        daily = fetched.get(stock.symbol)
        if daily is None:
            continue
        try:
            changed += _upsert_daily_rows(
                db,
                stock=stock,
                rows=list(daily.data),
                provider=daily.provider,
                latest_completed_date=latest_completed,
            )
            db.commit()
        except Exception:
            db.rollback()
            errors += 1
    return {
        "quote_updated": quote_result.updated,
        "quote_missing": len(quote_result.missing),
        "quote_errors": 0,
        "daily_symbols": len(selected_by_id),
        "daily_rows": changed,
        "daily_errors": errors,
        "errors": errors,
    }
