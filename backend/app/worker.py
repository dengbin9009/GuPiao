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
from .providers import corporate_event_router, market_router, trading_calendar_service
from .probability_portfolio.config import PROBABILITY_PORTFOLIO_DEFAULTS
from .probability_portfolio.market_snapshot import sync_probability_market_data
from .probability_portfolio.candidates import build_scored_candidates
from .probability_portfolio.observation import (
    finalize_probability_training_samples,
    pending_observation_symbols,
    record_probability_observation,
)
from .probability_portfolio.training import train_and_store_probability_model
from .runtime_bootstrap import seed_strategy_runtimes
from .services import seed_database
from .trading_agents.market_snapshot import sync_agent_market_data

LOGGER = logging.getLogger("gupiao.worker")
SHANGHAI = ZoneInfo("Asia/Shanghai")
AGENT_SNAPSHOT_RETRY_SECONDS = 120
PROBABILITY_SNAPSHOT_RETRY_SECONDS = 15


def quote_poll_scope(current: datetime | None = None) -> str | None:
    current = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return None
    current_time = current.time().replace(tzinfo=None)
    if (
        wall_time(9, 30) <= current_time <= wall_time(10, 0)
        or wall_time(10, 30) <= current_time <= wall_time(10, 45)
    ):
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


def probability_snapshot_scope(current: datetime | None = None) -> bool:
    current = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return False
    current_time = current.time().replace(tzinfo=None)
    return wall_time(14, 40) <= current_time < wall_time(14, 40, 30)


def probability_preheat_scope(current: datetime | None = None) -> bool:
    current = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return False
    current_time = current.time().replace(tzinfo=None)
    return wall_time(13, 40) <= current_time < wall_time(14, 10)


def probability_observation_scope(current: datetime | None = None) -> bool:
    current = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return False
    current_time = current.time().replace(tzinfo=None)
    return wall_time(14, 40) <= current_time < wall_time(14, 41)


def probability_label_scope(current: datetime | None = None) -> bool:
    current = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return False
    current_time = current.time().replace(tzinfo=None)
    return wall_time(10, 30) <= current_time <= wall_time(10, 45)


def _probability_config(db):
    return db.scalar(
        select(StrategyConfig)
        .join(StrategyDefinition)
        .where(
            StrategyDefinition.key == "overnight_probability_portfolio",
            StrategyConfig.enabled.is_(True),
            StrategyConfig.mode == "SIMULATION",
        )
        .limit(1)
    )


def poll_probability_market_snapshot(
    *,
    router=None,
    session_factory=SessionLocal,
    current: datetime | None = None,
    completed_at: datetime | None = None,
    record_observation: bool = True,
) -> dict[str, int]:
    current = current or datetime.now(SHANGHAI)
    with session_factory() as db:
        config = _probability_config(db)
        if config is None:
            return {"skipped": 1}
        try:
            result = sync_probability_market_data(
                db,
                config,
                router or market_router(),
                current=current,
            )
            completed_at = completed_at or datetime.now(SHANGHAI)
            observation_window = current.replace(
                hour=14,
                minute=40,
                second=0,
                microsecond=0,
            )
            observation_deadline = observation_window + timedelta(minutes=1)
            if (
                record_observation
                and not result.get("errors")
                and completed_at.date() == current.date()
                and observation_window <= completed_at < observation_deadline
            ):
                candidates = build_scored_candidates(
                    db,
                    config,
                    current=completed_at,
                )
                observation = record_probability_observation(
                    db,
                    config,
                    current=completed_at,
                    scored_candidates=candidates.scored,
                    rejected_candidates=candidates.rejected,
                    candidate_reasons=candidates.reasons,
                )
                result = {**result, "observation_run_id": observation.id}
            return result
        except Exception as exc:
            db.rollback()
            LOGGER.exception("概率组合行情固化失败")
            return {"skipped": 0, "errors": 1, "message": str(exc)[:200]}


def poll_probability_observation(
    *,
    session_factory=SessionLocal,
    current: datetime | None = None,
) -> dict[str, int]:
    current = current or datetime.now(SHANGHAI)
    with session_factory() as db:
        config = _probability_config(db)
        if config is None:
            return {"skipped": 1}
        candidates = build_scored_candidates(db, config, current=current)
        run = record_probability_observation(
            db,
            config,
            current=current,
            scored_candidates=candidates.scored,
            rejected_candidates=candidates.rejected,
            candidate_reasons=candidates.reasons,
        )
        return {
            "accepted": int((run.summary or {}).get("accepted", 0)),
        }


def poll_due_probability_observation(
    *,
    last_observation_date,
    current: datetime | None = None,
):
    current = current or datetime.now(SHANGHAI)
    if (
        not probability_observation_scope(current)
        or last_observation_date == current.date()
    ):
        return last_observation_date, None
    return current.date(), poll_probability_observation(current=current)


def poll_probability_training_labels(
    *,
    provider=None,
    session_factory=SessionLocal,
    current: datetime | None = None,
) -> dict[str, int]:
    current = current or datetime.now(SHANGHAI)
    with session_factory() as db:
        config = _probability_config(db)
        if config is None:
            return {"skipped": 1}
        calendar = trading_calendar_service()
        try:
            symbols = pending_observation_symbols(
                db,
                config,
                current=current,
                calendar=calendar,
            )
        except Exception:
            db.rollback()
            return {
                "created": 0,
                "skipped": 0,
                "errors": 1,
                "quote_updated": 0,
            }
        if not symbols:
            return {
                "created": 0,
                "skipped": 0,
                "errors": 0,
                "quote_updated": 0,
            }
        try:
            quote_result = _refresh_symbol_quotes(db, symbols, provider)
        except Exception:
            db.rollback()
            return {
                "created": 0,
                "skipped": 0,
                "errors": 1,
                "quote_updated": 0,
            }
        result = finalize_probability_training_samples(
            db,
            config,
            current=current,
            calendar=calendar,
        )
        output = {**result, "quote_updated": quote_result.updated}
        if result["created"] or result["skipped"]:
            parameters = {
                **PROBABILITY_PORTFOLIO_DEFAULTS,
                **(config.parameters or {}),
            }
            artifact = train_and_store_probability_model(
                db,
                through=current.date(),
                feature_version=str(parameters["feature_version"]),
                min_training_samples=int(parameters["min_training_samples"]),
                min_calibration_samples=int(parameters["min_calibration_samples"]),
                max_brier_score=float(parameters["max_brier_score"]),
            )
            output.update(
                {
                    "model_artifact_id": artifact.id,
                    "model_status": artifact.status,
                    "training_sample_count": artifact.training_sample_count,
                    "calibration_sample_count": artifact.calibration_sample_count,
                }
            )
        return output


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
        seed_strategy_runtimes(db, get_settings())
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
    last_probability_snapshot_date = None
    last_probability_snapshot_attempt = None
    last_probability_preheat_date = None
    last_probability_preheat_attempt = None
    last_probability_observation_date = None
    last_probability_label_date = None
    last_probability_label_attempt = None
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
            if (
                probability_preheat_scope(current_dt)
                and last_probability_preheat_date != current_dt.date()
                and (
                    last_probability_preheat_attempt is None
                    or current - last_probability_preheat_attempt
                    >= PROBABILITY_SNAPSHOT_RETRY_SECONDS
                )
            ):
                result = poll_probability_market_snapshot(
                    current=current_dt,
                    record_observation=False,
                )
                last_probability_preheat_attempt = time.time()
                if not result.get("errors"):
                    last_probability_preheat_date = current_dt.date()
                LOGGER.info("概率组合数据预热 result=%s", result)
            if (
                probability_snapshot_scope(current_dt)
                and last_probability_snapshot_date != current_dt.date()
                and (
                    last_probability_snapshot_attempt is None
                    or current - last_probability_snapshot_attempt
                    >= PROBABILITY_SNAPSHOT_RETRY_SECONDS
                )
            ):
                result = poll_probability_market_snapshot(current=current_dt)
                last_probability_snapshot_attempt = time.time()
                if not result.get("errors"):
                    last_probability_snapshot_date = current_dt.date()
                LOGGER.info("概率组合行情固化 result=%s", result)
            observed_date, result = poll_due_probability_observation(
                last_observation_date=last_probability_observation_date,
            )
            if result is not None:
                last_probability_observation_date = observed_date
                LOGGER.info("概率组合无下单观察 result=%s", result)
            if (
                probability_label_scope(current_dt)
                and last_probability_label_date != current_dt.date()
                and (
                    last_probability_label_attempt is None
                    or current - last_probability_label_attempt
                    >= PROBABILITY_SNAPSHOT_RETRY_SECONDS
                )
            ):
                result = poll_probability_training_labels(current=current_dt)
                last_probability_label_attempt = time.time()
                if not result.get("errors"):
                    last_probability_label_date = current_dt.date()
                LOGGER.info("概率组合训练标签 result=%s", result)
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
