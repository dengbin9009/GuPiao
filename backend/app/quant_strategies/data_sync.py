from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..models import (
    FinancialReportSnapshot,
    MarketDailyBar,
    MarketDailyMetric,
    Position,
    Stock,
    StrategyConfig,
    StrategyDefinition,
)
from .catalog import DEFAULT_ETF_UNIVERSE, QUANT_STRATEGY_SPECS


ETF_STRATEGY_KEYS = {"regime_allocator", "risk_parity_overlay"}


def quant_sync_stock_universe(
    db: Session,
    *,
    limit: int = 800,
) -> list[Stock]:
    """Return the liquid stock sync pool plus every open quant holding."""
    ranked_bars = (
        select(
            MarketDailyBar.stock_id.label("stock_id"),
            MarketDailyBar.amount.label("amount"),
            func.row_number()
            .over(
                partition_by=MarketDailyBar.stock_id,
                order_by=MarketDailyBar.trade_date.desc(),
            )
            .label("row_number"),
        )
        .where(MarketDailyBar.quality_status == "valid")
        .subquery()
    )
    average_amounts = (
        select(
            ranked_bars.c.stock_id,
            func.avg(ranked_bars.c.amount).label("average_amount"),
            func.count(ranked_bars.c.amount).label("bar_count"),
        )
        .where(ranked_bars.c.row_number <= 20)
        .group_by(ranked_bars.c.stock_id)
        .subquery()
    )
    liquidity = case(
        (
            average_amounts.c.bar_count >= 20,
            average_amounts.c.average_amount,
        ),
        else_=Stock.turnover_amount,
    )
    selected = list(
        db.scalars(
            select(Stock)
            .outerjoin(
                average_amounts,
                average_amounts.c.stock_id == Stock.id,
            )
            .where(
                Stock.status == "active",
                Stock.exchange.in_(["SSE", "SZSE"]),
                Stock.instrument_type == "STOCK",
                liquidity.is_not(None),
            )
            .order_by(
                liquidity.desc(),
                Stock.symbol,
            )
            .limit(max(1, min(int(limit), 800)))
        )
    )

    quant_account_ids = (
        select(StrategyConfig.simulation_account_id)
        .join(
            StrategyDefinition,
            StrategyDefinition.id == StrategyConfig.strategy_definition_id,
        )
        .where(
            StrategyDefinition.key.in_(tuple(QUANT_STRATEGY_SPECS)),
            StrategyConfig.mode == "SIMULATION",
            StrategyConfig.simulation_account_id.is_not(None),
        )
    )
    held_ids = (
        select(Position.stock_id)
        .where(
            Position.mode == "SIMULATION",
            Position.quantity > 0,
            Position.account_id.in_(quant_account_ids),
        )
        .distinct()
    )
    held = list(
        db.scalars(
            select(Stock)
            .where(
                Stock.id.in_(held_ids),
                Stock.exchange.in_(["SSE", "SZSE"]),
                Stock.instrument_type == "STOCK",
            )
            .order_by(Stock.symbol)
        )
    )
    selected_ids = {stock.id for stock in selected}
    return [*selected, *(stock for stock in held if stock.id not in selected_ids)]


def configured_etf_symbols(db: Session) -> tuple[str, ...]:
    symbols: set[str] = set()
    rows = db.execute(
        select(StrategyConfig, StrategyDefinition)
        .join(
            StrategyDefinition,
            StrategyDefinition.id == StrategyConfig.strategy_definition_id,
        )
        .where(StrategyDefinition.key.in_(tuple(ETF_STRATEGY_KEYS)))
    )
    for config, _definition in rows:
        symbols.update(
            str(symbol).upper()
            for symbol in (
                (config.parameters or {}).get("etf_universe")
                or DEFAULT_ETF_UNIVERSE
            )
        )
    return tuple(sorted(symbols or set(DEFAULT_ETF_UNIVERSE)))


def configured_benchmark_symbols(db: Session) -> tuple[str, ...]:
    symbols: set[str] = set()
    rows = db.execute(
        select(StrategyConfig, StrategyDefinition)
        .join(
            StrategyDefinition,
            StrategyDefinition.id == StrategyConfig.strategy_definition_id,
        )
        .where(StrategyDefinition.key.in_(tuple(QUANT_STRATEGY_SPECS)))
    )
    for config, _definition in rows:
        symbol = (config.parameters or {}).get("benchmark_symbol")
        if symbol:
            symbols.add(str(symbol).upper())
    return tuple(sorted(symbols))


def _field(row: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        value = row.get(name)
        if value not in {None, ""}:
            return value
    return default


def _date_text(value: Any) -> str:
    text = str(value or "").strip().replace("/", "-")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10]


def sync_daily_rows(
    db: Session,
    stock: Stock,
    rows: Iterable[dict[str, Any]],
    *,
    source: str,
    amount_multiplier: float = 1,
    volume_multiplier: float = 1,
) -> int:
    rows = list(rows)
    trade_dates = {
        trade_date
        for row in rows
        if (trade_date := _date_text(_field(row, "trade_date", "date", "日期")))
    }
    existing = {
        bar.trade_date: bar
        for bar in db.scalars(
            select(MarketDailyBar).where(
                MarketDailyBar.stock_id == stock.id,
                MarketDailyBar.trade_date.in_(trade_dates),
            )
        )
    } if trade_dates else {}
    changed = 0
    for row in rows:
        trade_date = _date_text(_field(row, "trade_date", "date", "日期"))
        close = float(_field(row, "close", "收盘", default=0) or 0)
        if not trade_date or close <= 0:
            continue
        bar = existing.get(trade_date)
        if bar is None:
            bar = MarketDailyBar(
                stock_id=stock.id,
                trade_date=trade_date,
                open=close,
                high=close,
                low=close,
                close=close,
                source=source,
            )
            db.add(bar)
            existing[trade_date] = bar
        bar.open = float(_field(row, "open", "开盘", default=close) or close)
        bar.high = float(_field(row, "high", "最高", default=close) or close)
        bar.low = float(_field(row, "low", "最低", default=close) or close)
        bar.close = close
        bar.volume = float(
            _field(row, "volume", "vol", "成交量", default=0) or 0
        ) * volume_multiplier
        bar.amount = float(
            _field(row, "amount", "成交额", default=0) or 0
        ) * amount_multiplier
        if stock.instrument_type == "ETF":
            bar.adjustment_factor = 1
            bar.adjusted_close = close
        else:
            # The daily payload is raw. A matching factor sync must validate it again.
            bar.adjustment_factor = None
            bar.adjusted_close = None
        bar.quality_status = "valid"
        bar.source = source
        changed += 1
    db.commit()
    return changed


def sync_etf_master_rows(
    db: Session,
    rows: Iterable[dict[str, Any]],
) -> int:
    changed = 0
    for row in rows:
        symbol = str(_field(row, "ts_code", "symbol", "代码", default="")).upper()
        name = str(_field(row, "name", "名称", default="")).strip()
        if not symbol or not name or not symbol.endswith((".SH", ".SZ")):
            continue
        code, suffix = symbol.split(".", 1)
        stock = db.scalar(select(Stock).where(Stock.symbol == symbol))
        if stock is None:
            stock = Stock(
                code=code,
                exchange="SSE" if suffix == "SH" else "SZSE",
                symbol=symbol,
                name=name,
            )
            db.add(stock)
        stock.name = name
        stock.status = "active"
        stock.instrument_type = "ETF"
        stock.lot_size = int(row.get("lot_size") or 100)
        stock.settlement_days = int(row.get("settlement_days") or 1)
        listing_date = _date_text(_field(row, "list_date", "listing_date"))
        if listing_date:
            stock.listing_date = listing_date
        changed += 1
    db.commit()
    return changed


def financial_available_on(
    announcement_date: str,
    *,
    trading_days: set[str] | None = None,
) -> str:
    announcement = date.fromisoformat(announcement_date[:10])
    if trading_days is not None:
        candidates = sorted(
            date.fromisoformat(day)
            for day in trading_days
            if announcement < date.fromisoformat(day) <= announcement + timedelta(days=14)
        )
        if candidates:
            return candidates[0].isoformat()
        raise ValueError("公告日之后缺少交易所交易日历")
    value = announcement + timedelta(days=1)
    while value.weekday() >= 5:
        value += timedelta(days=1)
    return value.isoformat()


def sync_adjustment_rows(
    db: Session,
    stock: Stock,
    rows: Iterable[dict[str, Any]],
    *,
    source: str,
) -> int:
    rows = list(rows)
    trade_dates = {
        str(row.get("trade_date", ""))[:10]
        for row in rows
        if row.get("trade_date")
    }
    existing = {
        bar.trade_date: bar
        for bar in db.scalars(
            select(MarketDailyBar).where(
                MarketDailyBar.stock_id == stock.id,
                MarketDailyBar.trade_date.in_(trade_dates),
            )
        )
    } if trade_dates else {}
    changed = 0
    for row in rows:
        trade_date = str(row.get("trade_date", ""))[:10]
        factor = row.get("adjustment_factor")
        if not trade_date or factor in {None, ""} or float(factor) <= 0:
            continue
        bar = existing.get(trade_date)
        if bar is None:
            continue
        bar.adjustment_factor = float(factor)
        bar.adjusted_close = float(bar.close) * float(factor)
        changed += 1
    db.commit()
    return changed


def sync_metric_rows(
    db: Session,
    stock: Stock,
    rows: Iterable[dict[str, Any]],
    *,
    source: str,
) -> int:
    rows = list(rows)
    trade_dates = {
        str(row.get("trade_date", ""))[:10]
        for row in rows
        if row.get("trade_date")
    }
    existing = {
        metric.trade_date: metric
        for metric in db.scalars(
            select(MarketDailyMetric).where(
                MarketDailyMetric.stock_id == stock.id,
                MarketDailyMetric.trade_date.in_(trade_dates),
            )
        )
    } if trade_dates else {}
    changed = 0
    for row in rows:
        trade_date = str(row.get("trade_date", ""))[:10]
        if not trade_date:
            continue
        metric = existing.get(trade_date)
        if metric is None:
            metric = MarketDailyMetric(
                stock_id=stock.id,
                trade_date=trade_date,
                source=source,
            )
            db.add(metric)
            existing[trade_date] = metric
        for name in (
            "pe_ttm",
            "pb",
            "dividend_yield",
            "total_market_value",
            "float_market_value",
        ):
            if name in row:
                setattr(metric, name, row.get(name))
        metric.source = source
        changed += 1
    db.commit()
    return changed


def sync_financial_rows(
    db: Session,
    stock: Stock,
    rows: Iterable[dict[str, Any]],
    *,
    source: str,
    trading_days: set[str] | None = None,
) -> int:
    rows = list(rows)
    keys = {
        (
            str(row.get("report_period", ""))[:10],
            str(
                row.get("actual_announcement_date")
                or row.get("announcement_date")
                or ""
            )[:10],
        )
        for row in rows
    }
    periods = {period for period, actual in keys if period and actual}
    actual_dates = {actual for period, actual in keys if period and actual}
    existing = {
        (snapshot.report_period, snapshot.actual_announcement_date): snapshot
        for snapshot in db.scalars(
            select(FinancialReportSnapshot).where(
                FinancialReportSnapshot.stock_id == stock.id,
                FinancialReportSnapshot.report_period.in_(periods),
                FinancialReportSnapshot.actual_announcement_date.in_(actual_dates),
            )
        )
    } if periods and actual_dates else {}
    changed = 0
    first_trading_day = (
        date.fromisoformat(min(trading_days)) if trading_days else None
    )
    for row in rows:
        period = str(row.get("report_period", ""))[:10]
        actual = str(
            row.get("actual_announcement_date")
            or row.get("announcement_date")
            or ""
        )[:10]
        if not period or not actual:
            continue
        if (
            first_trading_day
            and date.fromisoformat(actual) + timedelta(days=14)
            < first_trading_day
        ):
            continue
        snapshot = existing.get((period, actual))
        if snapshot is None:
            earliest_available = financial_available_on(
                actual,
                trading_days=trading_days,
            )
            snapshot = FinancialReportSnapshot(
                stock_id=stock.id,
                report_period=period,
                announcement_date=str(row.get("announcement_date") or actual)[:10],
                actual_announcement_date=actual,
                available_on=earliest_available,
                source=source,
            )
            db.add(snapshot)
            existing[(period, actual)] = snapshot
        else:
            earliest_available = financial_available_on(
                actual,
                trading_days=trading_days,
            )
        snapshot.report_type = str(row.get("report_type") or "quarterly")
        provided_available = str(row.get("available_on") or "")[:10]
        snapshot.available_on = max(
            earliest_available,
            provided_available or earliest_available,
        )
        for name in (
            "eps",
            "roe",
            "gross_margin",
            "operating_cash_flow",
            "net_profit",
            "revenue",
            "total_assets",
            "total_liabilities",
        ):
            if name in row:
                setattr(snapshot, name, row.get(name))
        snapshot.source = source
        changed += 1
    db.commit()
    return changed
