from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (
    AccountSnapshot,
    SimulationAccount,
    SimulationAccountLedger,
    StrategyConfig,
    StrategyDefinition,
    StrategySchedule,
)
from .config import TRADING_AGENTS_DEFAULTS, configuration_fingerprint, readiness
from ..models import TradingAgentBatch


def simulation_account_is_available(
    db: Session,
    *,
    account_id: int | None,
    strategy_config_id: int | None,
) -> bool:
    if account_id is None:
        return False
    account = db.get(SimulationAccount, account_id)
    if account is None or account.status != "active":
        return False
    occupied = db.scalar(
        select(StrategyConfig.id).where(
            StrategyConfig.simulation_account_id == account_id,
            StrategyConfig.id != strategy_config_id,
        )
    )
    return occupied is None


def seed_trading_agents_runtime(db: Session, settings: Settings) -> StrategyConfig:
    definition = db.scalar(
        select(StrategyDefinition).where(
            StrategyDefinition.key == "trading_agents_auto"
        )
    )
    if definition is None:
        raise RuntimeError("缺少 TradingAgents 策略定义")
    account = db.scalar(
        select(SimulationAccount).where(
            SimulationAccount.name == "TradingAgents 模拟账户"
        )
    )
    if account is None:
        account = SimulationAccount(
            name="TradingAgents 模拟账户",
            initial_cash=100_000,
            cash_balance=100_000,
            available_cash=100_000,
            total_asset=100_000,
            commission_rate=settings.simulation_commission_rate,
            min_commission=settings.simulation_min_commission,
            stamp_tax_rate=settings.simulation_stamp_tax_rate,
            transfer_fee_rate=settings.simulation_transfer_fee_rate,
            slippage_bps=settings.simulation_slippage_bps,
        )
        db.add(account)
        db.flush()
        db.add(
            SimulationAccountLedger(
                simulation_account_id=account.id,
                event_type="initialize",
                amount=account.initial_cash,
                balance_after=account.initial_cash,
                message="系统创建 TradingAgents 独立模拟账户",
            )
        )
        db.add(
            AccountSnapshot(
                mode="SIMULATION",
                account_id=account.id,
                cash_balance=account.cash_balance,
                available_cash=account.available_cash,
                frozen_cash=account.frozen_cash,
                market_value=0,
                total_asset=account.total_asset,
                realized_pnl=0,
                unrealized_pnl=0,
                exposure=0,
                source="trading_agents_simulated_broker",
            )
        )
    config = db.scalar(
        select(StrategyConfig).where(
            StrategyConfig.strategy_definition_id == definition.id,
            StrategyConfig.mode == "SIMULATION",
        )
    )
    if config is None:
        config = StrategyConfig(
            strategy_definition_id=definition.id,
            name="TradingAgents AI 自动组合",
            mode="SIMULATION",
            parameters=dict(TRADING_AGENTS_DEFAULTS),
            enabled=True,
            simulation_account_id=account.id,
        )
        db.add(config)
        db.flush()
    else:
        config.mode = "SIMULATION"
        if not simulation_account_is_available(
            db,
            account_id=config.simulation_account_id,
            strategy_config_id=config.id,
        ):
            config.simulation_account_id = account.id
    for trigger_type, run_time in (
        ("agent_analysis", "13:30"),
        ("agent_rebalance", "14:45"),
    ):
        if not db.scalar(
            select(StrategySchedule.id).where(
                StrategySchedule.strategy_config_id == config.id,
                StrategySchedule.trigger_type == trigger_type,
            )
        ):
            db.add(
                StrategySchedule(
                    strategy_config_id=config.id,
                    trigger_type=trigger_type,
                    run_time=run_time,
                    enabled=False,
                )
            )
    runtime_ready = readiness(settings)["ready"]
    dry_run_ready = find_matching_dry_run(db, config) is not None
    automation_mode = not bool((config.parameters or {}).get("dry_run", True))
    if not (runtime_ready and dry_run_ready and automation_mode):
        for schedule in db.scalars(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id,
                StrategySchedule.trigger_type.in_(["agent_analysis", "agent_rebalance"]),
            )
        ):
            schedule.enabled = False
            schedule.last_scheduled_for = None
            schedule.next_run_at = None
    db.commit()
    return config


def find_matching_dry_run(
    db: Session,
    config: StrategyConfig,
) -> TradingAgentBatch | None:
    fingerprint = configuration_fingerprint(
        config.parameters,
        simulation_account_id=config.simulation_account_id,
    )
    return db.scalar(
        select(TradingAgentBatch)
        .where(
            TradingAgentBatch.strategy_config_id == config.id,
            TradingAgentBatch.status == "dry_run_completed",
            TradingAgentBatch.config_fingerprint == fingerprint,
        )
        .order_by(TradingAgentBatch.id.desc())
        .limit(1)
    )
