from __future__ import annotations

from datetime import date, datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    QuantPortfolioDecision,
    StrategyConfig,
    StrategyDefinition,
    StrategyRun,
    TradingAgentBatch,
    now,
)
from .services import execute_simulation_exit, execute_simulation_strategy
from .probability_portfolio.execution import (
    execute_portfolio_entry,
    execute_portfolio_exit,
)
from .probability_portfolio.candidates import build_scored_candidates
from .trading_agents.batches import create_batch
from .trading_agents.rebalance import rebalance_batch
from .quant_strategies.catalog import QUANT_STRATEGY_SPECS
from .quant_strategies.tasks import enqueue_task
from .quant_strategies.schedule import adjacent_trading_day, should_generate_signal


class ProbabilityDataPendingError(RuntimeError):
    pass


PROBABILITY_PENDING_DATA_REASONS = {
    "公司事件数据未就绪或已过期",
    "缺少真实上市日期",
    "缺少真实换手率",
    "缺少真实成交额",
    "缺少真实日内VWAP",
    "缺少尾盘30分钟收益",
    "缺少当日开高低数据",
    "最新价格无效",
    "行情或因子时间缺失",
    "行情时间位于未来",
    "行情已过期",
    "行情来源不健康",
    "日线包含未完成或未来数据",
    "已完成日线不足20根",
    "基准日线包含未完成或未来数据",
    "基准已完成日线不足5根",
}


def execute_portfolio_entry_trigger(
    db: Session,
    config: StrategyConfig,
    *,
    current: datetime,
) -> StrategyRun:
    candidates = build_scored_candidates(db, config, current=current)
    rejected_reasons = {
        reason
        for item in candidates.rejected
        for reason in item.reasons
    }
    if not candidates.scored and (
        set(candidates.reasons) | rejected_reasons
    ) & PROBABILITY_PENDING_DATA_REASONS:
        raise ProbabilityDataPendingError("概率组合决策数据仍在准备")
    return execute_portfolio_entry(
        db,
        config,
        current=current,
        scored_candidates=candidates.scored,
        rejected_candidates=candidates.rejected,
        candidate_reasons=candidates.reasons,
    )


def strategy_key(db: Session, config: StrategyConfig) -> str:
    definition = db.get(StrategyDefinition, config.strategy_definition_id)
    if not definition:
        raise ValueError("策略定义不存在")
    return definition.key


def _agent_analysis(
    db: Session,
    config: StrategyConfig,
    *,
    current: datetime,
) -> StrategyRun:
    batch = create_batch(db, config, current=current)
    run = StrategyRun(
        strategy_config_id=config.id,
        mode="SIMULATION",
        status="completed",
        finished_at=current,
        summary={
            "accepted": 1,
            "batch_id": batch.id,
            "batch_status": batch.status,
        },
    )
    db.add(run)
    db.commit()
    return run


def _agent_rebalance(
    db: Session,
    config: StrategyConfig,
    *,
    current: datetime,
) -> StrategyRun:
    batch = db.scalar(
        select(TradingAgentBatch).where(
            TradingAgentBatch.strategy_config_id == config.id,
            TradingAgentBatch.trading_date == current.date().isoformat(),
        )
    )
    if batch and batch.status == "ready":
        return rebalance_batch(db, batch, current=current)
    run = StrategyRun(
        strategy_config_id=config.id,
        mode="SIMULATION",
        status="completed",
        finished_at=current,
        summary={
            "accepted": 0,
            "batch_id": batch.id if batch else None,
            "reason": "TradingAgents 批次尚未完成",
            "retryable": bool(batch and batch.status in {"pending", "processing"}),
        },
    )
    db.add(run)
    db.commit()
    return run


def _quant_task(
    db: Session,
    config: StrategyConfig,
    *,
    current: datetime,
    trigger_type: str,
    trading_day_fn: Callable[[date], bool],
) -> StrategyRun:
    task_type = "signal" if trigger_type == "quant_signal" else "execute"
    key = strategy_key(db, config)
    spec = QUANT_STRATEGY_SPECS[key]
    if task_type == "signal":
        next_day = adjacent_trading_day(
            current.date(),
            direction=1,
            trading_day_fn=trading_day_fn,
        )
        if next_day is None:
            raise RuntimeError("未来交易日历不可用")
        if not should_generate_signal(
            spec.rebalance_frequency,
            current.date(),
            next_trading_day=next_day,
        ):
            run = StrategyRun(
                strategy_config_id=config.id,
                mode="SIMULATION",
                status="completed",
                started_at=current,
                finished_at=current,
                summary={
                    "accepted": 0,
                    "queued": False,
                    "reason": f"{spec.rebalance_frequency}策略本交易日无需生成信号",
                },
            )
            db.add(run)
            db.commit()
            return run
    else:
        previous_day = adjacent_trading_day(
            current.date(),
            direction=-1,
            trading_day_fn=trading_day_fn,
        )
        if previous_day is None:
            raise RuntimeError("上一交易日历不可用")
        next_sellable_day = adjacent_trading_day(
            current.date(),
            direction=1,
            trading_day_fn=trading_day_fn,
        )
        if next_sellable_day is None:
            raise RuntimeError("未来交易日历不可用")
        decision = db.scalar(
            select(QuantPortfolioDecision)
            .where(
                QuantPortfolioDecision.strategy_config_id == config.id,
                QuantPortfolioDecision.decision_type == "signal",
                QuantPortfolioDecision.status == "ready",
                QuantPortfolioDecision.trading_date == previous_day.isoformat(),
            )
            .order_by(QuantPortfolioDecision.trading_date.desc())
            .limit(1)
        )
        if decision is None:
            run = StrategyRun(
                strategy_config_id=config.id,
                mode="SIMULATION",
                status="completed",
                started_at=current,
                finished_at=current,
                summary={
                    "accepted": 0,
                    "queued": False,
                    "reason": "没有上一交易日待执行的组合决策",
                },
            )
            db.add(run)
            db.commit()
            return run
    task = enqueue_task(
        db,
        config.id,
        task_type,
        current.date().isoformat(),
        payload=(
            {
                "decision_id": decision.id,
                "expected_signal_date": previous_day.isoformat(),
                "next_sellable_date": next_sellable_day.isoformat(),
            }
            if task_type == "execute"
            else None
        ),
        idempotency_suffix=(
            f"decision-{decision.id}" if task_type == "execute" else None
        ),
        deadline_at=(
            current.replace(hour=10, minute=0, second=0, microsecond=0)
            if task_type == "execute"
            else current.replace(hour=23, minute=0, second=0, microsecond=0)
        ),
        max_attempts=100 if task_type == "execute" else 1000,
    )
    run = StrategyRun(
        strategy_config_id=config.id,
        mode="SIMULATION",
        status="completed",
        started_at=current,
        finished_at=current,
        summary={
            "accepted": 1,
            "queued": True,
            "task_id": task.id,
            "task_type": task_type,
            "task_status": task.status,
        },
    )
    db.add(run)
    db.commit()
    return run


def execute_strategy_trigger(
    db: Session,
    config: StrategyConfig,
    trigger_type: str,
    *,
    current: datetime | None = None,
    trading_day_fn: Callable[[date], bool] | None = None,
    overnight_entry_executor: Callable[[Session, StrategyConfig], StrategyRun] = execute_simulation_strategy,
    overnight_exit_executor: Callable[[Session, StrategyConfig], StrategyRun] = execute_simulation_exit,
) -> StrategyRun:
    current = current or now()
    trading_day_fn = trading_day_fn or (lambda day: day.weekday() < 5)
    key = strategy_key(db, config)
    registry: dict[tuple[str, str], Callable[[], StrategyRun]] = {
        ("overnight_hold", "entry_evaluation"): lambda: overnight_entry_executor(db, config),
        ("overnight_hold", "exit_evaluation"): lambda: overnight_exit_executor(db, config),
        ("trading_agents_auto", "agent_analysis"): lambda: _agent_analysis(
            db, config, current=current
        ),
        ("trading_agents_auto", "agent_rebalance"): lambda: _agent_rebalance(
            db, config, current=current
        ),
        ("overnight_probability_portfolio", "portfolio_entry"): lambda: (
            execute_portfolio_entry_trigger(db, config, current=current)
        ),
        ("overnight_probability_portfolio", "portfolio_exit"): lambda: (
            execute_portfolio_exit(db, config, current=current)
        ),
    }
    if key in QUANT_STRATEGY_SPECS and trigger_type in {"quant_signal", "quant_execute"}:
        return _quant_task(
            db,
            config,
            current=current,
            trigger_type=trigger_type,
            trading_day_fn=trading_day_fn,
        )
    execute = registry.get((key, trigger_type))
    if not execute:
        raise ValueError(f"策略 {key} 不支持触发类型 {trigger_type}")
    return execute()
