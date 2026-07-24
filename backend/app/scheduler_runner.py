from __future__ import annotations

import logging
import time
from datetime import datetime, time as wall_time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select, update

from .database import SessionLocal
from .models import (
    ProbabilityPortfolioRun,
    StrategyConfig,
    StrategyRun,
    StrategySchedule,
    now,
)
from .providers import trading_calendar_service
from .scheduler import evaluate_schedule
from .services import execute_simulation_exit, execute_simulation_strategy
from .strategy_execution import execute_strategy_trigger
from .runtime_bootstrap import wait_for_runtime_database

LOGGER = logging.getLogger("gupiao.scheduler")
RETRY_DELAY = timedelta(seconds=15)
CLAIM_LEASE = timedelta(seconds=45)


def schedule_run_needs_retry(run) -> bool:
    return run.status == "completed" and bool((run.summary or {}).get("retryable"))


def retry_is_due(next_run_at: datetime | None, *, current: datetime) -> bool:
    if next_run_at is None:
        return True
    if next_run_at.tzinfo is None:
        next_run_at = next_run_at.replace(tzinfo=current.tzinfo)
    return current >= next_run_at


def schedule_tolerance_seconds(schedule, config: StrategyConfig) -> int:
    if schedule.trigger_type in {"quant_signal", "quant_execute"}:
        run_time = wall_time.fromisoformat(schedule.run_time)
        deadline = (
            wall_time(23, 0)
            if schedule.trigger_type == "quant_signal"
            else wall_time(10, 0)
        )
        run_seconds = run_time.hour * 3600 + run_time.minute * 60 + run_time.second
        deadline_seconds = deadline.hour * 3600 + deadline.minute * 60
        return max(59, deadline_seconds - run_seconds)
    if schedule.trigger_type == "agent_analysis":
        run_time = wall_time.fromisoformat(schedule.run_time)
        deadline_text = str((config.parameters or {}).get("analysis_deadline", "14:42"))
        deadline = wall_time.fromisoformat(deadline_text)
        run_seconds = run_time.hour * 3600 + run_time.minute * 60 + run_time.second
        deadline_seconds = deadline.hour * 3600 + deadline.minute * 60 + deadline.second
        return max(59, deadline_seconds - run_seconds)
    if schedule.trigger_type == "agent_rebalance":
        run_time = wall_time.fromisoformat(schedule.run_time)
        latest_text = str((config.parameters or {}).get("latest_rebalance_time", "14:50"))
        latest = wall_time.fromisoformat(latest_text)
        run_seconds = run_time.hour * 3600 + run_time.minute * 60 + run_time.second
        latest_seconds = latest.hour * 3600 + latest.minute * 60 + latest.second
        return max(59, latest_seconds - run_seconds)
    if schedule.trigger_type == "portfolio_exit":
        run_time = wall_time.fromisoformat(schedule.run_time)
        latest_text = str((config.parameters or {}).get("latest_exit_time", "10:45"))
        latest = wall_time.fromisoformat(latest_text)
        run_seconds = run_time.hour * 3600 + run_time.minute * 60 + run_time.second
        latest_seconds = latest.hour * 3600 + latest.minute * 60 + latest.second
        return max(59, latest_seconds - run_seconds)
    if schedule.trigger_type == "portfolio_entry":
        run_time = wall_time.fromisoformat(schedule.run_time)
        latest_text = str((config.parameters or {}).get("latest_entry_time", "14:41"))
        latest = wall_time.fromisoformat(latest_text)
        run_seconds = run_time.hour * 3600 + run_time.minute * 60 + run_time.second
        latest_seconds = latest.hour * 3600 + latest.minute * 60 + latest.second
        return max(59, latest_seconds - run_seconds)
    if schedule.trigger_type != "exit_evaluation":
        return 59
    run_time = wall_time.fromisoformat(schedule.run_time)
    latest_text = str((config.parameters or {}).get("latest_exit_time", "10:00"))
    latest = wall_time.fromisoformat(latest_text)
    run_seconds = run_time.hour * 3600 + run_time.minute * 60 + run_time.second
    latest_seconds = latest.hour * 3600 + latest.minute * 60 + latest.second
    return max(59, latest_seconds - run_seconds)


def claim_schedule_window(
    db,
    *,
    schedule_id: int,
    window_key: str,
    current: datetime,
) -> bool:
    lease_until = current + CLAIM_LEASE
    claimable = or_(
        StrategySchedule.last_scheduled_for.is_(None),
        StrategySchedule.last_scheduled_for != window_key,
        and_(
            StrategySchedule.last_scheduled_for == window_key,
            StrategySchedule.next_run_at.is_not(None),
            StrategySchedule.next_run_at <= current,
        ),
    )
    result = db.execute(
        update(StrategySchedule)
        .where(
            StrategySchedule.id == schedule_id,
            StrategySchedule.enabled.is_(True),
            claimable,
        )
        .values(
            last_scheduled_for=window_key,
            next_run_at=lease_until,
            updated_at=now(),
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def existing_window_run(
    db,
    *,
    schedule: StrategySchedule,
    current: datetime,
) -> StrategyRun | None:
    expected_quant_task = {
        "quant_signal": "signal",
        "quant_execute": "execute",
    }.get(schedule.trigger_type)

    def matches_trigger(run: StrategyRun | None) -> bool:
        if run is None:
            return False
        if schedule.trigger_type in {"portfolio_entry", "portfolio_exit"}:
            return db.scalar(
                select(ProbabilityPortfolioRun.id).where(
                    ProbabilityPortfolioRun.strategy_run_id == run.id,
                    ProbabilityPortfolioRun.trigger_type == schedule.trigger_type,
                )
            ) is not None
        if expected_quant_task:
            summary = run.summary or {}
            return bool(
                summary.get("queued") is True
                and summary.get("task_type") == expected_quant_task
                and summary.get("task_id")
            )
        return True

    target = wall_time.fromisoformat(schedule.run_time)
    window_start = datetime.combine(current.date(), target, tzinfo=current.tzinfo)
    if schedule.last_run_id:
        linked = db.get(StrategyRun, schedule.last_run_id)
        if linked:
            linked_started_at = linked.started_at
            if linked_started_at.tzinfo is None:
                linked_started_at = linked_started_at.replace(tzinfo=current.tzinfo)
            if (
                matches_trigger(linked)
                and window_start <= linked_started_at <= current
                and not schedule_run_needs_retry(linked)
            ):
                return linked
    query = select(StrategyRun)
    if schedule.trigger_type in {"portfolio_entry", "portfolio_exit"}:
        query = query.join(
            ProbabilityPortfolioRun,
            ProbabilityPortfolioRun.strategy_run_id == StrategyRun.id,
        ).where(ProbabilityPortfolioRun.trigger_type == schedule.trigger_type)
    candidate = db.scalar(
        query.where(
            StrategyRun.strategy_config_id == schedule.strategy_config_id,
            StrategyRun.started_at >= window_start,
            StrategyRun.started_at <= current,
        )
        .order_by(StrategyRun.id.desc())
        .limit(1)
    )
    if (
        candidate
        and matches_trigger(candidate)
        and not schedule_run_needs_retry(candidate)
    ):
        return candidate
    return None


def run_due_schedules(current: datetime | None = None) -> int:
    current = current or datetime.now(ZoneInfo("Asia/Shanghai"))
    calendar = trading_calendar_service()
    executed = 0
    with SessionLocal() as db:
        schedules = list(
            db.scalars(
                select(StrategySchedule)
                .where(StrategySchedule.enabled.is_(True))
                .order_by(StrategySchedule.id)
            )
        )
        for schedule in schedules:
            config = db.get(StrategyConfig, schedule.strategy_config_id)
            if not config or not config.enabled:
                continue
            if not retry_is_due(schedule.next_run_at, current=current):
                continue
            effective_last_scheduled_for = schedule.last_scheduled_for
            if (
                effective_last_scheduled_for
                and schedule.next_run_at is not None
                and retry_is_due(schedule.next_run_at, current=current)
            ):
                effective_last_scheduled_for = None
            decision = evaluate_schedule(
                trigger_type=schedule.trigger_type,
                run_time=schedule.run_time,
                enabled=schedule.enabled,
                last_scheduled_for=effective_last_scheduled_for,
                current=current,
                tolerance_seconds=schedule_tolerance_seconds(schedule, config),
                trading_day_fn=lambda day: calendar.is_trading_day(
                    day,
                    allow_weekday_fallback=config.mode != "LIVE",
                ),
            )
            if not decision.should_run:
                continue
            schedule_id = schedule.id
            if not claim_schedule_window(
                db,
                schedule_id=schedule_id,
                window_key=decision.window_key,
                current=current,
            ):
                continue
            schedule = db.get(StrategySchedule, schedule_id)
            reconciled_run = existing_window_run(
                db,
                schedule=schedule,
                current=current,
            )
            if reconciled_run:
                schedule.last_run_id = reconciled_run.id
                schedule.next_run_at = None
                schedule.updated_at = now()
                db.commit()
                LOGGER.info(
                    "自动计划恢复已完成运行 trigger=%s run_id=%s",
                    schedule.trigger_type,
                    reconciled_run.id,
                )
                continue
            try:
                run = execute_strategy_trigger(
                    db,
                    config,
                    schedule.trigger_type,
                    current=current,
                    trading_day_fn=lambda day: calendar.is_trading_day(
                        day,
                        allow_weekday_fallback=config.mode != "LIVE",
                    ),
                    overnight_entry_executor=execute_simulation_strategy,
                    overnight_exit_executor=execute_simulation_exit,
                )
            except Exception:
                db.rollback()
                schedule = db.get(StrategySchedule, schedule.id)
                schedule.last_scheduled_for = None
                schedule.next_run_at = current + RETRY_DELAY
                schedule.updated_at = now()
                db.commit()
                LOGGER.exception(
                    "自动计划执行异常 trigger=%s，将在窗口内重试",
                    schedule.trigger_type,
                )
                continue
            schedule.last_run_id = run.id
            if schedule_run_needs_retry(run):
                schedule.last_scheduled_for = None
                schedule.next_run_at = current + RETRY_DELAY
            else:
                schedule.next_run_at = None
            schedule.updated_at = now()
            db.commit()
            LOGGER.info(
                "自动计划执行 trigger=%s run_id=%s status=%s accepted=%s retryable=%s reason=%s",
                schedule.trigger_type,
                run.id,
                run.status,
                (run.summary or {}).get("accepted"),
                schedule_run_needs_retry(run),
                (run.summary or {}).get("reason"),
            )
            executed += 1
    return executed


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    wait_for_runtime_database()
    LOGGER.info("自动调度已启动")
    while True:
        try:
            run_due_schedules()
        except Exception:
            LOGGER.exception("自动调度迭代失败，将在下一轮继续")
        time.sleep(5)


if __name__ == "__main__":
    main()
