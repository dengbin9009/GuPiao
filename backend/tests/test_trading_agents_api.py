from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
import pytest
from fastapi import HTTPException

from app.main import (
    ScheduleUpdate,
    StrategyConfigCreate,
    TradingAgentsConfigUpdate,
    create_strategy_config,
    update_schedule,
    simulation_accounts,
    update_trading_agents_config,
)
from app.models import SimulationAccount, StrategyConfig, StrategyDefinition, StrategySchedule
from app.services import seed_database
from app.trading_agents.config import TRADING_AGENTS_DEFAULTS
from app.trading_agents.runtime import seed_trading_agents_runtime


def test_updating_agent_config_disables_both_schedules(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'agents-api.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_trading_agents_runtime(db, settings)
        schedules = list(
            db.scalars(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id == config.id
                )
            )
        )
        for schedule in schedules:
            schedule.enabled = True
            schedule.last_scheduled_for = "old-window"
        db.commit()

        update_trading_agents_config(
            TradingAgentsConfigUpdate(
                parameters={**TRADING_AGENTS_DEFAULTS, "deep_model": "gpt-5.4"},
                simulation_account_id=config.simulation_account_id,
            ),
            None,
            db,
        )

        assert all(not item.enabled for item in schedules)
        assert all(item.last_scheduled_for is None for item in schedules)


def test_generic_api_never_creates_trading_agents_live_config(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'agents-live.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.main.require_live_runtime_open", lambda: None)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)

        with pytest.raises(HTTPException) as exc_info:
            create_strategy_config(
                StrategyConfigCreate(
                    strategy_key="trading_agents_auto",
                    name="禁止的实盘配置",
                    mode="LIVE",
                    parameters={},
                ),
                None,
                db,
            )

    assert exc_info.value.status_code == 422
    assert "仅支持模拟盘" in str(exc_info.value.detail)


def test_trading_agents_schedule_type_and_time_are_fixed(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'agents-schedule.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_trading_agents_runtime(db, settings)
        schedule = db.scalar(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id,
                StrategySchedule.trigger_type == "agent_analysis",
            )
        )

        with pytest.raises(HTTPException) as exc_info:
            update_schedule(
                schedule.id,
                ScheduleUpdate(run_time="13:31"),
                None,
                db,
            )

    assert exc_info.value.status_code == 422
    assert "固定" in str(exc_info.value.detail)


def test_agent_config_rejects_account_bound_to_another_strategy(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'exclusive-account.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        agent_config = seed_trading_agents_runtime(db, settings)
        overnight = db.scalar(
            select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold")
        )
        shared = SimulationAccount(
            name="已占用账户",
            initial_cash=100_000,
            cash_balance=100_000,
            available_cash=100_000,
            total_asset=100_000,
            commission_rate=0.0003,
            min_commission=5,
            stamp_tax_rate=0.0005,
            transfer_fee_rate=0,
            slippage_bps=5,
        )
        db.add(shared)
        db.flush()
        db.add(
            StrategyConfig(
                strategy_definition_id=overnight.id,
                name="其他策略",
                mode="SIMULATION",
                parameters={},
                simulation_account_id=shared.id,
            )
        )
        db.commit()

        with pytest.raises(HTTPException) as exc_info:
            update_trading_agents_config(
                TradingAgentsConfigUpdate(
                    parameters=TRADING_AGENTS_DEFAULTS,
                    simulation_account_id=shared.id,
                ),
                None,
                db,
            )

    assert exc_info.value.status_code == 422
    assert "独立账户" in str(exc_info.value.detail)
    assert agent_config.simulation_account_id != shared.id


def test_generic_simulation_config_binds_default_account(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'generic-account.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        default_account = db.scalar(
            select(SimulationAccount).order_by(SimulationAccount.id)
        )
        default_account_id = default_account.id
        config = create_strategy_config(
            StrategyConfigCreate(
                strategy_key="overnight_hold",
                name="绑定默认账户",
                mode="SIMULATION",
                parameters={},
            ),
            None,
            db,
        )

    assert config["simulation_account_id"] == default_account_id


def test_simulation_account_list_marks_strategy_exclusivity(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'account-options.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        seed_trading_agents_runtime(db, settings)
        accounts = simulation_accounts(None, db)

    by_name = {item["name"]: item for item in accounts}
    assert by_name["默认模拟账户"]["available_for_trading_agents"] is True
    assert by_name["TradingAgents 模拟账户"]["available_for_trading_agents"] is True
    assert by_name["TradingAgents 模拟账户"]["bound_strategy_keys"] == [
        "trading_agents_auto"
    ]
