from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import (
    ProbabilityCandidateDecision,
    ProbabilityModelArtifact,
    ProbabilityPortfolioRun,
    ProbabilityTrainingSample,
    SimulationAccount,
    StrategyConfig,
    StrategyDefinition,
    StrategyPositionLot,
    StrategySchedule,
)
from app.probability_portfolio.runtime import seed_probability_portfolio_runtime
from app.runtime_bootstrap import seed_strategy_runtimes
from app.services import seed_database


def test_probability_portfolio_models_and_runtime_are_simulation_only(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'probability.db'}")
    Base.metadata.create_all(engine)
    settings = Settings(database_url=str(engine.url))

    with Session(engine) as db:
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)

        definition = db.scalar(
            select(StrategyDefinition).where(
                StrategyDefinition.key == "overnight_probability_portfolio"
            )
        )
        account = db.get(SimulationAccount, config.simulation_account_id)
        schedules = list(
            db.scalars(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id == config.id
                )
            )
        )

        assert definition is not None
        assert definition.name == "一夜持股概率组合"
        assert definition.required_timeframes == ["1d", "1m", "realtime"]
        assert account is not None
        assert account.name == "一夜持股概率组合模拟账户"
        assert account.initial_cash == 2_000_000
        assert account.cash_balance == 2_000_000
        assert config.mode == "SIMULATION"
        assert config.simulation_account_id == account.id
        assert config.parameters["max_positions"] == 10
        assert config.parameters["min_position_pct"] == 0.02
        assert config.parameters["max_position_pct"] == 0.36
        assert config.parameters["max_total_exposure_pct"] == 0.60
        assert config.parameters["exit_time"] == "10:30"
        assert {item.trigger_type: item.run_time for item in schedules} == {
            "portfolio_entry": "14:40",
            "portfolio_exit": "10:30",
        }
        assert all(not item.enabled for item in schedules)

        assert ProbabilityModelArtifact.__tablename__ in Base.metadata.tables
        assert ProbabilityTrainingSample.__tablename__ in Base.metadata.tables
        assert ProbabilityPortfolioRun.__tablename__ in Base.metadata.tables
        assert "config_fingerprint" in ProbabilityPortfolioRun.__table__.columns
        assert ProbabilityCandidateDecision.__tablename__ in Base.metadata.tables
        assert StrategyPositionLot.__tablename__ in Base.metadata.tables


def test_probability_runtime_restart_preserves_account_balance_and_disables_entry(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'restart.db'}")
    Base.metadata.create_all(engine)
    settings = Settings(database_url=str(engine.url))

    with Session(engine) as db:
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        account = db.get(SimulationAccount, config.simulation_account_id)
        account.cash_balance = 1_900_000
        account.available_cash = 1_900_000
        entry = db.scalar(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id,
                StrategySchedule.trigger_type == "portfolio_entry",
            )
        )
        entry.enabled = True
        db.commit()

        restarted = seed_probability_portfolio_runtime(db, settings)
        db.expire_all()
        account = db.get(SimulationAccount, restarted.simulation_account_id)
        entry = db.scalar(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == restarted.id,
                StrategySchedule.trigger_type == "portfolio_entry",
            )
        )

        assert account.cash_balance == 1_900_000
        assert account.initial_cash == 2_000_000
        assert entry.enabled is False


def test_probability_runtime_uses_an_exclusive_account(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'exclusive.db'}")
    Base.metadata.create_all(engine)
    settings = Settings(database_url=str(engine.url))

    with Session(engine) as db:
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        dedicated_id = config.simulation_account_id
        overnight = db.scalar(
            select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold")
        )
        db.add(
            StrategyConfig(
                strategy_definition_id=overnight.id,
                name="错误共享配置",
                mode="SIMULATION",
                parameters={},
                simulation_account_id=dedicated_id,
            )
        )
        db.commit()

        restarted = seed_probability_portfolio_runtime(db, settings)

        assert restarted.simulation_account_id != dedicated_id
        replacement = db.get(SimulationAccount, restarted.simulation_account_id)
        assert replacement.initial_cash == 2_000_000


def test_strategy_runtime_bootstrap_seeds_probability_portfolio(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bootstrap.db'}")
    Base.metadata.create_all(engine)
    settings = Settings(database_url=str(engine.url))

    with Session(engine) as db:
        seed_database(db, settings)
        seeded = seed_strategy_runtimes(db, settings)

        probability = db.scalar(
            select(StrategyDefinition).where(
                StrategyDefinition.key == "overnight_probability_portfolio"
            )
        )
        trading_agents = db.scalar(
            select(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )

        assert probability is not None
        assert trading_agents is not None
        assert seeded["probability_portfolio"].strategy_definition_id == probability.id
        assert seeded["trading_agents"].strategy_definition_id == trading_agents.id
