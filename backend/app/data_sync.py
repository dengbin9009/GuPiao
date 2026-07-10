from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .market_data import normalize_events
from .models import DataSourceState, Stock, StockEvent, now


@dataclass(frozen=True)
class SyncResult:
    created: int = 0
    updated: int = 0
    missing: list[str] = field(default_factory=list)


def pinyin_metadata(name: str) -> tuple[str, str]:
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return "", ""
    full = "".join(lazy_pinyin(name, errors="ignore"))
    initials = "".join(lazy_pinyin(name, style=Style.FIRST_LETTER, errors="ignore"))
    return full.lower(), initials.lower()


def _value(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return default


def _symbol(row: dict[str, Any]) -> tuple[str, str, str]:
    raw = str(_value(row, "ts_code", "symbol", "代码", default="")).upper()
    code = str(_value(row, "code", "symbol", "代码", default=raw.split(".")[0])).split(".")[0]
    suffix = raw.split(".")[1] if "." in raw else ""
    exchange = str(_value(row, "exchange", "市场", default="")).upper()
    if exchange in {"SH", "SSE"} or suffix == "SH":
        return code, "SSE", f"{code}.SH"
    if exchange in {"SZ", "SZSE"} or suffix == "SZ":
        return code, "SZSE", f"{code}.SZ"
    if exchange in {"BJ", "BSE"} or suffix == "BJ":
        return code, "BSE", f"{code}.BJ"
    if code.startswith(("4", "8", "9")):
        return code, "BSE", f"{code}.BJ"
    if code.startswith(("5", "6", "7")):
        return code, "SSE", f"{code}.SH"
    return code, "SZSE", f"{code}.SZ"


def sync_stock_master(
    db: Session,
    provider: Any,
    *,
    pinyin_resolver: Callable[[str], tuple[str, str]] = pinyin_metadata,
) -> SyncResult:
    created = 0
    updated = 0
    for row in provider.stock_master():
        code, exchange, symbol = _symbol(row)
        name = str(_value(row, "name", "名称", default="")).strip()
        if not code or not name:
            continue
        pinyin, initials = pinyin_resolver(name)
        stock = db.scalar(select(Stock).where(Stock.symbol == symbol))
        if stock is None:
            stock = Stock(code=code, exchange=exchange, symbol=symbol, name=name)
            db.add(stock)
            created += 1
        else:
            updated += 1
        stock.code = code
        stock.exchange = exchange
        stock.name = name
        stock.pinyin = pinyin
        stock.pinyin_initials = initials
        stock.status = "active"
    _mark_provider(db, provider.name, healthy=True)
    db.commit()
    return SyncResult(created=created, updated=updated)


def refresh_quotes(db: Session, provider: Any, symbols: list[str]) -> SyncResult:
    rows = provider.quotes(symbols)
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in rows:
        _, _, symbol = _symbol(row)
        by_symbol[symbol] = row
    updated = 0
    missing: list[str] = []
    timestamp = now()
    for symbol in symbols:
        stock = db.scalar(select(Stock).where(Stock.symbol == symbol))
        if not stock:
            continue
        row = by_symbol.get(symbol)
        if row is None:
            stock.last_price = None
            stock.change_pct = None
            stock.turnover_amount = None
            stock.quote_updated_at = None
            missing.append(symbol)
            continue
        stock.last_price = float(_value(row, "last_price", "price", "最新价", "close", default=0))
        stock.change_pct = float(_value(row, "change_pct", "pct_chg", "涨跌幅", default=0))
        stock.turnover_amount = float(_value(row, "amount", "turnover_amount", "成交额", default=0))
        quote_at = _value(row, "quote_at", "trade_time")
        stock.quote_updated_at = quote_at if isinstance(quote_at, datetime) else timestamp
        updated += 1
    _mark_provider(db, provider.name, healthy=True, quote_at=timestamp)
    db.commit()
    return SyncResult(updated=updated, missing=missing)


def sync_corporate_events(db: Session, rows: Iterable[dict[str, Any]]) -> SyncResult:
    created = 0
    updated = 0
    for row in normalize_events(rows):
        stock = db.scalar(select(Stock).where(Stock.symbol == row.get("symbol")))
        if not stock:
            continue
        existing = db.scalar(
            select(StockEvent).where(
                StockEvent.source == row["source"],
                StockEvent.source_event_id == row["source_event_id"],
            )
        )
        if existing:
            updated += 1
            continue
        db.add(
            StockEvent(
                stock_id=stock.id,
                event_type=str(row["event_type"]),
                severity=str(row["severity"]),
                title=str(row["title"]),
                source=str(row["source"]),
                source_event_id=str(row["source_event_id"]),
                published_at=row.get("published_at") or now(),
                effective_at=row.get("effective_at"),
                unlock_free_float_pct=row.get("unlock_free_float_pct"),
                raw_uri=row.get("raw_uri"),
                fetched_at=now(),
            )
        )
        created += 1
    db.commit()
    return SyncResult(created=created, updated=updated)


def _mark_provider(
    db: Session,
    provider: str,
    *,
    healthy: bool,
    quote_at: datetime | None = None,
) -> None:
    state = db.scalar(select(DataSourceState).where(DataSourceState.provider == provider))
    if not state:
        return
    state.healthy = healthy
    state.last_checked_at = now()
    state.last_error = None if healthy else state.last_error
    if quote_at:
        state.last_quote_at = quote_at
