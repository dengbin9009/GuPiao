from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import (
    Administrator,
    DataSourceState,
    LiveTradingAccount,
    RiskSettings,
    SimulationAccount,
    Stock,
    StrategyConfig,
    StrategyDefinition,
    StrategySchedule,
    WatchlistItem,
)
from .security import hash_password, verify_password
from .services import OVERNIGHT_DEFAULTS, adjust_simulation_cash, seed_database


OBSERVE_CONFIG_NAME = "一夜持股法模拟盘观察"


def _assert_simulation_only(settings: Settings) -> None:
    if settings.live_enabled or settings.broker_adapter != "simulation":
        raise RuntimeError(
            "模拟盘观察模式要求 LIVE_TRADING_ENABLED=false 且 BROKER_ADAPTER=simulation"
        )


def prepare_simulation_runtime(db: Session, settings: Settings) -> dict[str, Any]:
    """Prepare the idempotent, simulation-only runtime state used by local observation."""
    _assert_simulation_only(settings)
    seed_database(db, settings)

    administrator = db.scalar(
        select(Administrator).where(
            Administrator.username == settings.admin_username
        )
    )
    if administrator and not verify_password(
        settings.admin_password,
        administrator.password_hash,
    ):
        administrator.password_hash = hash_password(settings.admin_password)

    live_risk = db.scalar(select(RiskSettings).where(RiskSettings.mode == "LIVE"))
    if live_risk:
        live_risk.live_enabled = False
        live_risk.emergency_stop_enabled = True
    for live_account in db.scalars(select(LiveTradingAccount)):
        live_account.enabled = False
        live_account.read_only = True
    simulation_risk = db.scalar(
        select(RiskSettings).where(RiskSettings.mode == "SIMULATION")
    )
    if simulation_risk:
        simulation_risk.emergency_stop_enabled = False

    for source in db.scalars(select(DataSourceState)):
        source.healthy = False
        source.last_quote_at = None
        source.last_checked_at = None
        source.last_error = "等待运行时实际探测"

    # Seed quotes are UI fixtures, not evidence that a tradable market quote was fetched.
    for stock in db.scalars(select(Stock)):
        stock.last_price = None
        stock.change_pct = None
        stock.turnover_amount = None
        stock.quote_updated_at = None

    account = db.scalar(select(SimulationAccount).limit(1))
    if account is None:
        raise RuntimeError("模拟盘初始化失败：缺少模拟账户")
    account.status = "active"
    funding_delta = float(settings.simulation_initial_cash) - float(account.initial_cash)
    if funding_delta:
        adjust_simulation_cash(db, account, funding_delta, "模拟盘观察初始资金调整")
        account.initial_cash = float(settings.simulation_initial_cash)

    definition = db.scalar(
        select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold")
    )
    target = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    if definition is None or target is None:
        raise RuntimeError("模拟盘初始化失败：缺少一夜持股法或默认观察股票")

    watchlist = db.scalar(select(WatchlistItem).where(WatchlistItem.stock_id == target.id))
    if watchlist is None:
        db.add(WatchlistItem(stock_id=target.id, note="模拟盘观察默认股票"))

    config = db.scalar(
        select(StrategyConfig).where(
            StrategyConfig.name == OBSERVE_CONFIG_NAME,
            StrategyConfig.mode == "SIMULATION",
        )
    )
    if config is None:
        config = StrategyConfig(
            strategy_definition_id=definition.id,
            name=OBSERVE_CONFIG_NAME,
            mode="SIMULATION",
            parameters=dict(OVERNIGHT_DEFAULTS),
            enabled=True,
        )
        db.add(config)
        db.flush()
    else:
        config.enabled = True

    schedule_specs = {
        "entry_evaluation": str(
            config.parameters.get("evaluation_time", OVERNIGHT_DEFAULTS["evaluation_time"])
        ),
        "exit_evaluation": str(
            config.parameters.get(
                "exit_evaluation_time", OVERNIGHT_DEFAULTS["exit_evaluation_time"]
            )
        ),
    }
    schedule_ids: list[int] = []
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
                enabled=True,
            )
            db.add(schedule)
            db.flush()
        else:
            schedule.run_time = run_time
            schedule.enabled = True
        schedule_ids.append(schedule.id)

    live_config_ids = select(StrategyConfig.id).where(StrategyConfig.mode == "LIVE")
    for schedule in db.scalars(
        select(StrategySchedule).where(StrategySchedule.strategy_config_id.in_(live_config_ids))
    ):
        schedule.enabled = False

    db.commit()
    return {
        "mode": "SIMULATION",
        "account_cash": float(account.initial_cash),
        "strategy_config_id": config.id,
        "schedule_ids": schedule_ids,
        "watchlist_symbol": target.symbol,
    }
