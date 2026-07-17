from __future__ import annotations

import logging
import time
from datetime import datetime, time as wall_time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .config import get_settings
from .database import Base, SessionLocal, apply_runtime_migrations, engine
from .data_sync import (
    mark_provider_failure,
    mark_provider_success,
    refresh_quotes,
    sync_corporate_events,
    sync_stock_master,
)
from .models import (
    NotificationChannel,
    NotificationDelivery,
    Position,
    Stock,
    StrategyConfig,
    StrategyDefinition,
    StrategySchedule,
    WatchlistItem,
    now,
)
from .notifications import deliver_channel
from .providers import corporate_event_router, market_router
from .services import seed_database
from .trading_agents.runtime import seed_trading_agents_runtime
from .trading_agents.market_snapshot import sync_agent_market_data

LOGGER = logging.getLogger("gupiao.worker")
SHANGHAI = ZoneInfo("Asia/Shanghai")
AGENT_SNAPSHOT_RETRY_SECONDS = 120


def quote_poll_scope(current: datetime | None = None) -> str | None:
    current = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return None
    current_time = current.time().replace(tzinfo=None)
    if wall_time(9, 30) <= current_time <= wall_time(10, 0):
        return "exit"
    if wall_time(14, 35) <= current_time <= wall_time(14, 41):
        return "entry"
    return None


def event_poll_scope(current: datetime | None = None) -> bool:
    current = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return False
    current_time = current.time().replace(tzinfo=None)
    return wall_time(14, 20) <= current_time < wall_time(14, 35)


def agent_snapshot_scope(current: datetime | None = None) -> bool:
    current = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return False
    current_time = current.time().replace(tzinfo=None)
    return wall_time(13, 25) <= current_time < wall_time(13, 30)


def agent_snapshot_retry_due(
    last_attempt_seconds: float | None,
    *,
    current_seconds: float,
) -> bool:
    return (
        last_attempt_seconds is None
        or current_seconds - last_attempt_seconds >= AGENT_SNAPSHOT_RETRY_SECONDS
    )


def poll_agent_market_snapshot(
    *,
    router=None,
    session_factory=SessionLocal,
    current: datetime | None = None,
) -> dict[str, int]:
    current = current or datetime.now(SHANGHAI)
    if router is None:
        event_result = poll_corporate_events(session_factory=session_factory)
        if event_result["errors"]:
            return {"skipped": 0, "errors": 1, "message": "公司公告同步失败"}
    with session_factory() as db:
        config = db.scalar(
            select(StrategyConfig)
            .join(StrategyDefinition)
            .where(
                StrategyDefinition.key == "trading_agents_auto",
                StrategyConfig.enabled.is_(True),
                StrategyConfig.mode == "SIMULATION",
            )
            .limit(1)
        )
        if not config:
            return {"skipped": 1}
        parameters = config.parameters or {}
        analysis_schedule_enabled = bool(
            db.scalar(
                select(StrategySchedule.id).where(
                    StrategySchedule.strategy_config_id == config.id,
                    StrategySchedule.trigger_type == "agent_analysis",
                    StrategySchedule.enabled.is_(True),
                )
            )
        )
        if not parameters.get("dry_run", True) and not analysis_schedule_enabled:
            return {"skipped": 1}
        try:
            return sync_agent_market_data(
                db,
                config,
                router or market_router(),
                current=current,
            )
        except Exception as exc:
            db.rollback()
            LOGGER.exception("TradingAgents 行情固化失败")
            return {"skipped": 0, "errors": 1, "message": str(exc)[:200]}


def should_poll_events(
    current: datetime,
    *,
    seconds_since_attempt: float,
    retry_seconds: int,
) -> bool:
    return (
        event_poll_scope(current)
        and seconds_since_attempt >= retry_seconds
    )


def notification_poll_allowed(current: datetime | None = None) -> bool:
    current = current or datetime.now(SHANGHAI)
    return quote_poll_scope(current) is None and not event_poll_scope(current)


def _refresh_symbol_quotes(db, symbols: list[str], provider=None):
    if provider is not None:
        selected_provider = provider
        rows = provider.quotes(symbols)
    else:
        routed = market_router().call("realtime", "quotes", symbols=symbols)
        selected_provider = type("SelectedProvider", (), {"name": routed.provider})()
        rows = list(routed.data)
    quote_provider = type(
        "QuoteRows",
        (),
        {"name": selected_provider.name, "quotes": lambda self, _: rows},
    )()
    return refresh_quotes(db, quote_provider, symbols)


def process_pending_notifications(limit: int = 20) -> int:
    processed = 0
    settings = get_settings()
    with SessionLocal() as db:
        pending = list(
            db.scalars(
                select(NotificationDelivery)
                .where(NotificationDelivery.status == "pending")
                .order_by(NotificationDelivery.id)
                .limit(limit)
            )
        )
        for delivery in pending:
            channel = db.get(NotificationChannel, delivery.channel_id)
            if not channel or not channel.enabled:
                delivery.status = "failed"
                delivery.last_error = "通知渠道不存在或未启用"
                processed += 1
                continue
            result = deliver_channel(
                settings,
                channel_type=channel.type,
                recipient=channel.recipient,
                secret_ref=channel.secret_ref,
                subject=delivery.subject,
                message=str(delivery.payload.get("message", delivery.payload)),
            )
            delivery.status = "sent" if result.sent else "failed"
            delivery.attempt_count = result.attempt_count
            delivery.last_error = result.last_error
            delivery.sent_at = now() if result.sent else None
            processed += 1
        db.commit()
    return processed


def poll_watchlist_quotes(provider=None, session_factory=SessionLocal) -> dict[str, int]:
    updated = 0
    missing = 0
    errors = 0
    with session_factory() as db:
        symbols = [
            item.symbol
            for item in db.scalars(
                select(Stock).join(WatchlistItem, WatchlistItem.stock_id == Stock.id).order_by(WatchlistItem.id)
            )
        ]
        if not symbols:
            return {"updated": 0, "missing": 0, "errors": 0}
        try:
            result = _refresh_symbol_quotes(db, symbols, provider)
            updated = result.updated
            missing = len(result.missing)
        except Exception as exc:
            db.rollback()
            try:
                mark_provider_failure(db, getattr(provider, "name", "akshare"), exc)
            except Exception:
                db.rollback()
                LOGGER.exception("自选行情失败状态写入失败")
            updated = 0
            missing = len(symbols)
            errors = 1
    return {"updated": updated, "missing": missing, "errors": errors}


def _strategy_symbols(db) -> list[str]:
    return list(
        db.scalars(
            select(Stock.symbol)
            .where(
                Stock.status == "active",
                Stock.exchange.in_(["SSE", "SZSE"]),
            )
            .order_by(Stock.symbol)
        )
    )


def poll_strategy_quotes(provider=None, session_factory=SessionLocal) -> dict[str, int]:
    with session_factory() as db:
        symbols = _strategy_symbols(db)
        if not symbols:
            return {"updated": 0, "missing": 0, "errors": 0}
        try:
            result = _refresh_symbol_quotes(db, symbols, provider)
            return {
                "updated": result.updated,
                "missing": len(result.missing),
                "errors": 0,
            }
        except Exception as exc:
            db.rollback()
            name = getattr(provider, "name", "market_router")
            try:
                mark_provider_failure(db, name, exc)
            except Exception:
                db.rollback()
                LOGGER.exception("全市场行情失败状态写入失败")
            return {"updated": 0, "missing": len(symbols), "errors": 1}


def poll_position_quotes(provider=None, session_factory=SessionLocal) -> dict[str, int]:
    with session_factory() as db:
        symbols = list(
            db.scalars(
                select(Stock.symbol)
                .join(Position, Position.stock_id == Stock.id)
                .where(
                    Position.mode == "SIMULATION",
                    Position.quantity > 0,
                )
                .order_by(Stock.symbol)
            )
        )
        if not symbols:
            return {"updated": 0, "missing": 0, "errors": 0}
        try:
            result = _refresh_symbol_quotes(db, symbols, provider)
            return {
                "updated": result.updated,
                "missing": len(result.missing),
                "errors": 0,
            }
        except Exception as exc:
            db.rollback()
            name = getattr(provider, "name", "market_router")
            try:
                mark_provider_failure(db, name, exc)
            except Exception:
                db.rollback()
                LOGGER.exception("持仓行情失败状态写入失败")
            return {"updated": 0, "missing": len(symbols), "errors": 1}


def poll_corporate_events(provider=None, session_factory=SessionLocal) -> dict[str, int]:
    with session_factory() as db:
        symbols = _strategy_symbols(db)
        if not symbols:
            return {"created": 0, "updated": 0, "errors": 0}
        end = now().date()
        start = end - timedelta(days=7)
        try:
            if provider is not None:
                selected_provider = provider
                rows = provider.events(
                    symbols=symbols,
                    start=start.isoformat(),
                    end=end.isoformat(),
                )
            else:
                routed = corporate_event_router().call(
                    "corporate_events",
                    "events",
                    symbols=symbols,
                    start=start.isoformat(),
                    end=end.isoformat(),
                )
                selected_provider = type(
                    "SelectedProvider",
                    (),
                    {"name": routed.provider},
                )()
                rows = list(routed.data)
            result = sync_corporate_events(db, rows)
            mark_provider_success(db, selected_provider.name)
            return {"created": result.created, "updated": result.updated, "errors": 0}
        except Exception as exc:
            db.rollback()
            name = getattr(provider, "name", "akshare_events")
            try:
                mark_provider_failure(db, name, exc)
            except Exception:
                db.rollback()
                LOGGER.exception("公告失败状态写入失败")
            return {"created": 0, "updated": 0, "errors": 1}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    Base.metadata.create_all(bind=engine)
    apply_runtime_migrations()
    with SessionLocal() as db:
        seed_database(db, get_settings())
        seed_trading_agents_runtime(db, get_settings())
        try:
            routed = market_router().call("stock_master", "stock_master")
            provider = type(
                "StockMasterRows",
                (),
                {
                    "name": routed.provider,
                    "stock_master": lambda self: list(routed.data),
                },
            )()
            sync_stock_master(db, provider)
            LOGGER.info("股票主数据同步完成 provider=%s", routed.provider)
        except Exception as exc:
            db.rollback()
            mark_provider_failure(db, "akshare", exc)
            LOGGER.exception("股票主数据同步失败")

    quote_result = poll_strategy_quotes()
    LOGGER.info("启动行情探测完成 result=%s", quote_result)
    event_result = poll_corporate_events()
    LOGGER.info("启动公告探测完成 result=%s", event_result)
    last_quote_poll = time.time()
    last_event_attempt = time.time()
    last_agent_snapshot_date = None
    last_agent_snapshot_attempt = None
    while True:
        try:
            current = time.time()
            current_dt = datetime.now(SHANGHAI)
            if (
                agent_snapshot_scope(current_dt)
                and last_agent_snapshot_date != current_dt.date()
                and agent_snapshot_retry_due(
                    last_agent_snapshot_attempt,
                    current_seconds=current,
                )
            ):
                result = poll_agent_market_snapshot(current=current_dt)
                last_agent_snapshot_attempt = time.time()
                if not result.get("errors"):
                    last_agent_snapshot_date = current_dt.date()
                LOGGER.info("TradingAgents 行情固化 result=%s", result)
            if notification_poll_allowed(current_dt):
                process_pending_notifications()
            scope = quote_poll_scope(current_dt)
            if scope and current - last_quote_poll >= get_settings().realtime_poll_seconds:
                result = (
                    poll_strategy_quotes()
                    if scope == "entry"
                    else poll_position_quotes()
                )
                last_quote_poll = time.time()
                LOGGER.info("%s 窗口行情刷新 result=%s", scope, result)
            if should_poll_events(
                current_dt,
                seconds_since_attempt=current - last_event_attempt,
                retry_seconds=get_settings().corporate_event_sync_seconds,
            ):
                result = poll_corporate_events()
                last_event_attempt = time.time()
                LOGGER.info("入场预热窗口公告刷新 result=%s", result)
        except Exception:
            LOGGER.exception("worker 迭代失败，将在下一轮继续")
        time.sleep(2)


if __name__ == "__main__":
    main()
