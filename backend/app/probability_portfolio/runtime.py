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
from .config import PROBABILITY_PORTFOLIO_DEFAULTS


STRATEGY_KEY = "overnight_probability_portfolio"
STRATEGY_NAME = "一夜持股概率组合"
ACCOUNT_NAME = "一夜持股概率组合模拟账户"
INITIAL_CASH = 2_000_000.0


def _account_is_available(
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


def _new_account(db: Session, settings: Settings) -> SimulationAccount:
    existing_count = len(
        list(
            db.scalars(
                select(SimulationAccount).where(
                    SimulationAccount.name.like(f"{ACCOUNT_NAME}%")
                )
            )
        )
    )
    name = ACCOUNT_NAME if existing_count == 0 else f"{ACCOUNT_NAME} #{existing_count + 1}"
    account = SimulationAccount(
        name=name,
        initial_cash=INITIAL_CASH,
        cash_balance=INITIAL_CASH,
        available_cash=INITIAL_CASH,
        total_asset=INITIAL_CASH,
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
            amount=INITIAL_CASH,
            balance_after=INITIAL_CASH,
            message="系统创建一夜持股概率组合独立模拟账户",
        )
    )
    db.add(
        AccountSnapshot(
            mode="SIMULATION",
            account_id=account.id,
            cash_balance=INITIAL_CASH,
            available_cash=INITIAL_CASH,
            frozen_cash=0,
            market_value=0,
            total_asset=INITIAL_CASH,
            realized_pnl=0,
            unrealized_pnl=0,
            exposure=0,
            source="probability_portfolio_simulated_broker",
        )
    )
    return account


def seed_probability_portfolio_runtime(
    db: Session,
    settings: Settings,
) -> StrategyConfig:
    definition = db.scalar(
        select(StrategyDefinition).where(StrategyDefinition.key == STRATEGY_KEY)
    )
    if definition is None:
        definition = StrategyDefinition(
            key=STRATEGY_KEY,
            name=STRATEGY_NAME,
            type="built_in",
            version="1.0.0",
            market="A_SHARE",
            parameter_schema={
                "type": "object",
                "properties": {
                    key: {"default": value}
                    for key, value in PROBABILITY_PORTFOLIO_DEFAULTS.items()
                },
            },
            signal_schema={"side": ["buy", "sell"], "required": ["symbol", "reason"]},
            required_timeframes=["1d", "1m", "realtime"],
        )
        db.add(definition)
        db.flush()

    config = db.scalar(
        select(StrategyConfig).where(
            StrategyConfig.strategy_definition_id == definition.id,
            StrategyConfig.mode == "SIMULATION",
        )
    )
    account = db.scalar(
        select(SimulationAccount)
        .where(SimulationAccount.name == ACCOUNT_NAME)
        .order_by(SimulationAccount.id)
    )
    if not _account_is_available(
        db,
        account_id=config.simulation_account_id if config else account.id if account else None,
        strategy_config_id=config.id if config else None,
    ):
        account = _new_account(db, settings)

    if config is None:
        config = StrategyConfig(
            strategy_definition_id=definition.id,
            name=STRATEGY_NAME,
            mode="SIMULATION",
            parameters=dict(PROBABILITY_PORTFOLIO_DEFAULTS),
            enabled=True,
            simulation_account_id=account.id,
        )
        db.add(config)
        db.flush()
    else:
        config.mode = "SIMULATION"
        config.enabled = True
        config.parameters = {
            **PROBABILITY_PORTFOLIO_DEFAULTS,
            **(config.parameters or {}),
        }
        if not _account_is_available(
            db,
            account_id=config.simulation_account_id,
            strategy_config_id=config.id,
        ):
            config.simulation_account_id = account.id

    schedule_specs = {
        "portfolio_entry": str(config.parameters["entry_time"]),
        "portfolio_exit": str(config.parameters["exit_time"]),
    }
    for trigger_type, run_time in schedule_specs.items():
        schedule = db.scalar(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id,
                StrategySchedule.trigger_type == trigger_type,
            )
        )
        if schedule is None:
            schedule = StrategySchedule(
                strategy_config_id=config.id,
                trigger_type=trigger_type,
                run_time=run_time,
                enabled=False,
            )
            db.add(schedule)
        else:
            schedule.run_time = run_time
            if trigger_type == "portfolio_entry":
                schedule.enabled = False
                schedule.last_scheduled_for = None
                schedule.next_run_at = None
    db.commit()
    return config
