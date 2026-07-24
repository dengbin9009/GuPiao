from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    QuantStrategyTask,
    StrategyConfig,
    StrategyRiskProfile,
    StrategySchedule,
    now,
)
from ..notifications import queue_notifications


RETRY_DELAY = timedelta(seconds=30)
TASK_LEASE_BY_TYPE = {
    "execute": timedelta(minutes=2),
    "signal": timedelta(minutes=15),
    "backtest": timedelta(hours=2),
}
TASK_PRIORITY = {
    "execute": 0,
    "signal": 1,
    "backtest": 2,
}


def enqueue_task(
    db: Session,
    strategy_config_id: int,
    task_type: str,
    trading_date: str,
    *,
    payload: dict[str, Any] | None = None,
    deadline_at: datetime | None = None,
    idempotency_suffix: str | None = None,
    max_attempts: int = 3,
) -> QuantStrategyTask:
    config = db.get(StrategyConfig, strategy_config_id)
    if config is None or config.mode != "SIMULATION" or not config.simulation_account_id:
        raise ValueError("独立量化任务必须绑定模拟账户")
    if task_type not in TASK_LEASE_BY_TYPE:
        raise ValueError("独立量化任务类型无效")
    idempotency_key = f"{strategy_config_id}:{trading_date}:{task_type}"
    if idempotency_suffix:
        idempotency_key = f"{strategy_config_id}:{task_type}:{idempotency_suffix}"
    existing = db.scalar(
        select(QuantStrategyTask).where(
            QuantStrategyTask.idempotency_key == idempotency_key
        )
    )
    if existing:
        return existing
    item = QuantStrategyTask(
        strategy_config_id=config.id,
        simulation_account_id=config.simulation_account_id,
        task_type=task_type,
        trading_date=trading_date,
        idempotency_key=idempotency_key,
        status="pending",
        payload=payload or {},
        deadline_at=deadline_at,
        max_attempts=max_attempts,
    )
    db.add(item)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return db.scalar(
            select(QuantStrategyTask).where(
                QuantStrategyTask.idempotency_key == idempotency_key
            )
        )
    db.refresh(item)
    return item


def claim_pending_task(
    db: Session,
    *,
    worker_id: str,
    current: datetime | None = None,
    strategy_config_id: int | None = None,
    task_types: set[str] | frozenset[str] | None = None,
) -> QuantStrategyTask | None:
    current = current or now()
    claimable = or_(
        and_(
            QuantStrategyTask.status.in_(["pending", "retry"]),
            or_(
                QuantStrategyTask.next_retry_at.is_(None),
                QuantStrategyTask.next_retry_at <= current,
            ),
        ),
        and_(
            QuantStrategyTask.status == "processing",
            QuantStrategyTask.lease_until.is_not(None),
            QuantStrategyTask.lease_until <= current,
        ),
    )
    filters = [
        claimable,
        QuantStrategyTask.attempts < QuantStrategyTask.max_attempts,
    ]
    if strategy_config_id is not None:
        filters.append(QuantStrategyTask.strategy_config_id == strategy_config_id)
    if task_types is not None:
        filters.append(QuantStrategyTask.task_type.in_(tuple(task_types)))
    candidate_id = db.scalar(
        select(QuantStrategyTask.id)
        .where(*filters)
        .order_by(
            *[
                (QuantStrategyTask.task_type == task_type).desc()
                for task_type, _priority in sorted(
                    TASK_PRIORITY.items(),
                    key=lambda item: item[1],
                )
            ],
            QuantStrategyTask.id,
        )
        .limit(1)
    )
    if candidate_id is None:
        return None
    candidate_type = db.scalar(
        select(QuantStrategyTask.task_type).where(
            QuantStrategyTask.id == candidate_id
        )
    )
    lease_duration = TASK_LEASE_BY_TYPE.get(
        str(candidate_type),
        timedelta(minutes=10),
    )
    result = db.execute(
        update(QuantStrategyTask)
        .where(QuantStrategyTask.id == candidate_id, *filters)
        .values(
            status="processing",
            worker_id=worker_id,
            lease_until=current + lease_duration,
            next_retry_at=None,
            attempts=QuantStrategyTask.attempts + 1,
            started_at=current,
            error_message=None,
            updated_at=now(),
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if result.rowcount != 1:
        return None
    return db.get(QuantStrategyTask, candidate_id)


def complete_task(
    db: Session,
    task: QuantStrategyTask,
    result: dict[str, Any],
    *,
    current: datetime | None = None,
) -> QuantStrategyTask:
    task.status = "completed"
    task.result = result
    task.completed_at = current or now()
    task.lease_until = None
    task.next_retry_at = None
    task.error_message = None
    risk = db.scalar(
        select(StrategyRiskProfile).where(
            StrategyRiskProfile.strategy_config_id == task.strategy_config_id
        )
    )
    if risk:
        risk.consecutive_errors = 0
    db.commit()
    return task


def fail_task(
    db: Session,
    task: QuantStrategyTask,
    error: Exception | str,
    *,
    retryable: bool,
    current: datetime | None = None,
) -> QuantStrategyTask:
    current = current or now()
    task.error_message = str(error)[:1000]
    task.lease_until = None
    risk = db.scalar(
        select(StrategyRiskProfile).where(
            StrategyRiskProfile.strategy_config_id == task.strategy_config_id
        )
    )
    deadline = task.deadline_at
    if deadline is not None and deadline.tzinfo is None and current.tzinfo is not None:
        deadline = deadline.replace(tzinfo=current.tzinfo)
    if deadline is not None and deadline.tzinfo is not None and current.tzinfo is None:
        current = current.replace(tzinfo=deadline.tzinfo)
    deadline_reached = bool(deadline and current >= deadline)
    exhausted = task.attempts >= task.max_attempts or deadline_reached
    final_failure = not retryable or exhausted
    count_error = task.deadline_at is None or final_failure
    if risk and count_error:
        risk.consecutive_errors += 1
    if retryable and not exhausted:
        task.status = "retry"
        task.next_retry_at = current + RETRY_DELAY
    else:
        task.status = "failed"
        task.completed_at = current
        task.next_retry_at = None
    auto_paused = bool(
        risk and risk.consecutive_errors >= risk.max_consecutive_errors
    )
    if auto_paused:
        task.status = "failed"
        task.completed_at = current
        task.next_retry_at = None
        for schedule in db.scalars(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == task.strategy_config_id
            )
        ):
            schedule.enabled = False
            schedule.next_run_at = None
    db.commit()
    if task.status == "failed":
        queue_notifications(
            db,
            event_type="quant_strategy_task_failed",
            severity="error",
            subject="独立量化策略任务失败",
            payload={
                "strategy_config_id": task.strategy_config_id,
                "task_id": task.id,
                "task_type": task.task_type,
                "error": task.error_message,
            },
        )
    if auto_paused:
        queue_notifications(
            db,
            event_type="quant_strategy_auto_paused",
            severity="critical",
            subject="独立量化策略已自动暂停",
            payload={
                "strategy_config_id": task.strategy_config_id,
                "task_id": task.id,
            },
        )
    return task
