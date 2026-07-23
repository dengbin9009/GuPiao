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
        listing_date = _value(row, "list_date", "listing_date", "上市时间", "上市日期")
        if listing_date is not None:
            text = str(listing_date).strip().replace("/", "-")
            if len(text) == 8 and text.isdigit():
                text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
            stock.listing_date = text[:10]
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
    quote_timestamps: list[datetime] = []
    for symbol in symbols:
        stock = db.scalar(select(Stock).where(Stock.symbol == symbol))
        if not stock:
            continue
        row = by_symbol.get(symbol)
        if row is None:
            stock.last_price = None
            stock.change_pct = None
            stock.turnover_amount = None
            stock.turnover_rate = None
            stock.open_price = None
            stock.high_price = None
            stock.low_price = None
            stock.volume = None
            stock.vwap = None
            stock.tail_30m_return = None
            stock.limit_up_price = None
            stock.limit_down_price = None
            stock.quote_source = None
            stock.quote_updated_at = None
            stock.factor_updated_at = None
            missing.append(symbol)
            continue
        stock.last_price = float(_value(row, "last_price", "price", "最新价", "close", default=0))
        stock.change_pct = float(_value(row, "change_pct", "pct_chg", "涨跌幅", default=0))
        stock.turnover_amount = float(_value(row, "amount", "turnover_amount", "成交额", default=0))
        turnover_rate = _value(row, "turnover_rate", "换手率")
        if turnover_rate is not None:
            value = float(turnover_rate)
            stock.turnover_rate = (
                value / 100
                if row.get("换手率") not in {None, ""} or abs(value) > 1
                else value
            )
        else:
            stock.turnover_rate = None
        stock.open_price = _optional_float(row, "open_price", "open", "今开")
        stock.high_price = _optional_float(row, "high_price", "high", "最高")
        stock.low_price = _optional_float(row, "low_price", "low", "最低")
        stock.volume = _optional_float(row, "volume", "vol")
        if stock.volume is None:
            chinese_volume = _optional_float(row, "成交量")
            stock.volume = chinese_volume * 100 if chinese_volume is not None else None
        if turnover_rate is None and stock.volume and stock.float_shares and stock.float_shares > 0:
            stock.turnover_rate = float(stock.volume) / float(stock.float_shares)
        stock.vwap = _optional_float(row, "vwap", "日内VWAP")
        if stock.vwap is None and stock.volume and stock.turnover_amount:
            stock.vwap = stock.turnover_amount / stock.volume
        incoming_tail_return = _optional_float(
            row, "tail_30m_return", "尾盘30分钟收益"
        )
        stock.limit_up_price = _optional_float(row, "limit_up_price", "涨停价")
        stock.limit_down_price = _optional_float(row, "limit_down_price", "跌停价")
        previous_close = _optional_float(row, "previous_close", "last_close", "昨收")
        if previous_close and previous_close > 0:
            limit_pct = 0.20 if stock.code.startswith(("300", "688")) else 0.10
            stock.limit_up_price = stock.limit_up_price or round(
                previous_close * (1 + limit_pct) + 1e-10,
                2,
            )
            stock.limit_down_price = stock.limit_down_price or round(
                previous_close * (1 - limit_pct) + 1e-10,
                2,
            )
        stock.quote_source = str(provider.name)
        quote_at = _value(row, "quote_at", "trade_time")
        stock.quote_updated_at = quote_at if isinstance(quote_at, datetime) else timestamp
        factor_at = stock.factor_updated_at
        if factor_at is not None and factor_at.tzinfo is None:
            factor_at = factor_at.replace(tzinfo=stock.quote_updated_at.tzinfo)
        same_factor_day = bool(
            factor_at and factor_at.date() == stock.quote_updated_at.date()
        )
        if incoming_tail_return is not None:
            stock.tail_30m_return = incoming_tail_return
            stock.factor_updated_at = stock.quote_updated_at
        elif not same_factor_day:
            stock.tail_30m_return = None
            stock.factor_updated_at = None
        quote_timestamps.append(stock.quote_updated_at)
        updated += 1
    latest_quote_at = max(quote_timestamps) if quote_timestamps else None
    _mark_provider(db, provider.name, healthy=True, quote_at=latest_quote_at)
    db.commit()
    return SyncResult(updated=updated, missing=missing)


def _optional_float(row: dict[str, Any], *keys: str) -> float | None:
    value = _value(row, *keys)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


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


def mark_provider_failure(db: Session, provider: str, error: Exception | str) -> None:
    state = db.scalar(select(DataSourceState).where(DataSourceState.provider == provider))
    if state:
        state.healthy = False
        state.last_checked_at = now()
        state.last_error = str(error)[:1000]
        db.commit()


def mark_provider_success(db: Session, provider: str) -> None:
    _mark_provider(db, provider, healthy=True)
    db.commit()


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
