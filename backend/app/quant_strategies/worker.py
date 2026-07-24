from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
import os
import socket
import time
from datetime import datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import QuantPortfolioDecision, StrategyConfig, StrategyDefinition
from ..runtime_bootstrap import wait_for_runtime_database
from .catalog import QUANT_STRATEGY_SPECS
from .execution import execute_quant_rebalance
from .backtest import run_quant_backtest
from .readiness import configuration_fingerprint, quant_strategy_readiness
from .signals import DataNotReadyError, build_signal_decision
from .tasks import claim_pending_task, complete_task, fail_task


LOGGER = logging.getLogger("gupiao.quant-strategy-worker")


@dataclass(frozen=True)
class WorkerLane:
    name: str
    strategy_config_id: int | None
    task_types: frozenset[str]


def worker_lane_specs(db: Session) -> list[WorkerLane]:
    strategy_ids = list(
        db.scalars(
            select(StrategyConfig.id)
            .join(StrategyDefinition)
            .where(StrategyDefinition.key.in_(tuple(QUANT_STRATEGY_SPECS)))
            .order_by(StrategyConfig.id)
        )
    )
    lanes = [
        WorkerLane(
            name=f"strategy-{strategy_id}",
            strategy_config_id=strategy_id,
            task_types=frozenset({"signal", "execute"}),
        )
        for strategy_id in strategy_ids
    ]
    lanes.extend(
        WorkerLane(
            name=f"backtest-{index + 1}",
            strategy_config_id=None,
            task_types=frozenset({"backtest"}),
        )
        for index in range(2)
    )
    return lanes


def _process_task(db: Session, task, *, current: datetime):
    config = db.get(StrategyConfig, task.strategy_config_id)
    if config is None:
        raise ValueError("独立量化策略配置不存在")
    if task.task_type == "signal":
        decision = build_signal_decision(db, config, current=current)
        return {
            "decision_id": decision.id,
            "decision_status": decision.status,
            "target_weights": decision.target_weights,
        }
    if task.task_type == "execute":
        readiness = quant_strategy_readiness(db, config.id)
        if not readiness["automation_ready"]:
            raise PermissionError(
                f"独立量化策略上线闸门未通过: {', '.join(readiness['reasons'])}"
            )
        decision_id = task.payload.get("decision_id")
        expected_signal_date = str(task.payload.get("expected_signal_date") or "")
        next_sellable_date = str(task.payload.get("next_sellable_date") or "")
        decision = db.get(QuantPortfolioDecision, decision_id) if decision_id else None
        if (
            decision is None
            or decision.strategy_config_id != config.id
            or decision.decision_type != "signal"
            or decision.status != "ready"
            or not expected_signal_date
            or not next_sellable_date
            or decision.trading_date != expected_signal_date
            or decision.trading_date >= task.trading_date
        ):
            decision = None
        if decision is None:
            raise DataNotReadyError("执行任务未绑定可用的上一交易日组合决策")
        run = execute_quant_rebalance(
            db,
            decision,
            current=current,
            dry_run=False,
            next_sellable_date=next_sellable_date,
        )
        if not run.summary.get("accepted") and run.summary.get("reason"):
            error = RuntimeError(str(run.summary["reason"]))
            if run.summary.get("retryable"):
                raise DataNotReadyError(str(error))
            raise error
        return {
            "decision_id": decision.id,
            "strategy_run_id": run.id,
            "order_ids": run.summary.get("order_ids", []),
        }
    if task.task_type == "backtest":
        definition = db.get(StrategyDefinition, config.strategy_definition_id)
        expected_fingerprint = configuration_fingerprint(
            config.parameters or {},
            simulation_account_id=config.simulation_account_id,
            strategy_version=definition.version,
        )
        if (
            task.payload.get("config_fingerprint") != expected_fingerprint
            or task.payload.get("strategy_version") != definition.version
            or str(task.payload.get("data_version"))
            != str((config.parameters or {}).get("data_version", "1"))
        ):
            raise PermissionError("回测任务排队后策略配置已变化")
        metrics, qualification = run_quant_backtest(
            db,
            config,
            start_date=str(task.payload["start_date"]),
            end_date=str(task.payload["end_date"]),
        )
        return {
            "qualification_id": qualification.id,
            "qualified": qualification.qualified,
            "trading_days": metrics.trading_days,
            "data_completeness": metrics.data_completeness,
            "annualized_return": metrics.annualized_return,
            "sharpe_ratio": metrics.sharpe_ratio,
            "max_drawdown": metrics.max_drawdown,
            "trade_count": metrics.trade_count,
        }
    raise ValueError(f"Worker 暂不支持任务类型 {task.task_type}")


def process_one(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    worker_id: str | None = None,
    current: datetime | None = None,
    strategy_config_id: int | None = None,
    task_types: frozenset[str] | None = None,
) -> dict[str, object]:
    worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    with session_factory() as db:
        task = claim_pending_task(
            db,
            worker_id=worker_id,
            current=current,
            strategy_config_id=strategy_config_id,
            task_types=task_types,
        )
        if task is None:
            return {"claimed": 0}
        task_id = task.id
        task_current = current or datetime.now().astimezone()
        try:
            result = _process_task(db, task, current=task_current)
        except PermissionError as exc:
            db.rollback()
            task = db.get(type(task), task_id)
            fail_task(db, task, exc, retryable=False, current=task_current)
        except DataNotReadyError as exc:
            db.rollback()
            task = db.get(type(task), task_id)
            fail_task(db, task, exc, retryable=True, current=task_current)
        except Exception as exc:
            db.rollback()
            task = db.get(type(task), task_id)
            fail_task(db, task, exc, retryable=True, current=task_current)
        else:
            complete_task(db, task, result, current=task_current)
        return {"claimed": 1, "task_id": task_id, "status": task.status}


def run_lane(lane: WorkerLane) -> None:
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{lane.name}"
    while True:
        try:
            result = process_one(
                worker_id=worker_id,
                strategy_config_id=lane.strategy_config_id,
                task_types=lane.task_types,
            )
            if result["claimed"]:
                LOGGER.info(
                    "独立量化任务处理 lane=%s task_id=%s status=%s",
                    lane.name,
                    result["task_id"],
                    result["status"],
                )
        except Exception:
            LOGGER.exception("独立量化通道失败 lane=%s，将在下一轮继续", lane.name)
        time.sleep(2)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    wait_for_runtime_database()
    with SessionLocal() as db:
        lanes = worker_lane_specs(db)
    LOGGER.info("独立量化策略 Worker 已启动 lanes=%s", len(lanes))
    with ThreadPoolExecutor(
        max_workers=len(lanes),
        thread_name_prefix="quant-strategy-lane",
    ) as executor:
        futures = [executor.submit(run_lane, lane) for lane in lanes]
        for future in futures:
            future.result()


if __name__ == "__main__":
    main()
