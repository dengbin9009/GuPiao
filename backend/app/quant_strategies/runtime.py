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
    StrategyRiskProfile,
    StrategySchedule,
)
from .catalog import QUANT_STRATEGY_SPECS


INITIAL_CASH = 2_000_000.0


def _account_name(strategy_name: str) -> str:
    return f"{strategy_name}模拟账户"


def _new_account(db: Session, settings: Settings, strategy_name: str) -> SimulationAccount:
    account = SimulationAccount(
        name=_account_name(strategy_name),
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
            message=f"系统创建{strategy_name}独立模拟账户",
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
            source="quant_strategy_simulated_broker",
        )
    )
    return account


def _definition(db: Session, key: str) -> StrategyDefinition | None:
    return db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == key))


def seed_quant_strategy_runtimes(
    db: Session,
    settings: Settings,
) -> dict[str, StrategyConfig]:
    if settings.live_enabled or settings.broker_adapter != "simulation":
        raise RuntimeError("独立量化策略仅允许在模拟盘安全配置下初始化")

    result: dict[str, StrategyConfig] = {}
    for key, spec in QUANT_STRATEGY_SPECS.items():
        definition = _definition(db, key)
        if definition is None:
            definition = StrategyDefinition(
                key=key,
                name=spec.name,
                type="built_in",
                version=spec.version,
                market="A_SHARE",
                parameter_schema={
                    "type": "object",
                    "properties": {
                        name: {"default": value}
                        for name, value in spec.defaults.items()
                    },
                },
                signal_schema={
                    "required": ["symbol", "target_weight", "reason"],
                    "side": ["buy", "sell", "hold"],
                },
                required_timeframes=["1d", "realtime"],
            )
            db.add(definition)
            db.flush()

        config = db.scalar(
            select(StrategyConfig).where(
                StrategyConfig.strategy_definition_id == definition.id
            )
        )
        if config is None:
            account = db.scalar(
                select(SimulationAccount).where(
                    SimulationAccount.name == _account_name(spec.name)
                )
            )
            if account is None:
                account = _new_account(db, settings, spec.name)
            config = StrategyConfig(
                strategy_definition_id=definition.id,
                name=spec.name,
                mode="SIMULATION",
                parameters=dict(spec.defaults),
                enabled=True,
                simulation_account_id=account.id,
            )
            db.add(config)
            db.flush()
        else:
            config.mode = "SIMULATION"
            if config.simulation_account_id is None:
                account = db.scalar(
                    select(SimulationAccount).where(
                        SimulationAccount.name == _account_name(spec.name)
                    )
                )
                if account is None:
                    account = _new_account(db, settings, spec.name)
                config.simulation_account_id = account.id

        for trigger_type, run_time in (
            ("quant_signal", spec.signal_time),
            ("quant_execute", spec.execution_time),
        ):
            schedule = db.scalar(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id == config.id,
                    StrategySchedule.trigger_type == trigger_type,
                )
            )
            if schedule is None:
                db.add(
                    StrategySchedule(
                        strategy_config_id=config.id,
                        trigger_type=trigger_type,
                        run_time=run_time,
                        enabled=False,
                    )
                )
            else:
                schedule.run_time = run_time

        risk = db.scalar(
            select(StrategyRiskProfile).where(
                StrategyRiskProfile.strategy_config_id == config.id
            )
        )
        if risk is None:
            db.add(
                StrategyRiskProfile(
                    strategy_config_id=config.id,
                    daily_loss_limit_pct=0.02,
                    max_drawdown_pct=0.15,
                    max_consecutive_errors=3,
                    max_daily_orders=max(20, spec.max_positions * 2),
                    max_order_notional_pct=spec.max_position_pct,
                )
            )
        result[key] = config

    db.commit()
    return result
