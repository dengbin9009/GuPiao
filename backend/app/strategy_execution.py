from __future__ import annotations

from datetime import datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
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


def execute_strategy_trigger(
    db: Session,
    config: StrategyConfig,
    trigger_type: str,
    *,
    current: datetime | None = None,
    overnight_entry_executor: Callable[[Session, StrategyConfig], StrategyRun] = execute_simulation_strategy,
    overnight_exit_executor: Callable[[Session, StrategyConfig], StrategyRun] = execute_simulation_exit,
) -> StrategyRun:
    current = current or now()
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
    execute = registry.get((key, trigger_type))
    if not execute:
        raise ValueError(f"策略 {key} 不支持触发类型 {trigger_type}")
    return execute()
