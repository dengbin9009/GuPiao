from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import (
    MarketDailyBar,
    SimulationAccount,
    StrategyConfig,
    StrategyDefinition,
    StrategySchedule,
    TradingAgentBatch,
    TradingAgentCandidateAnalysis,
    TradingAgentPortfolioDecision,
    SimulationAccountLedger,
)
from app.services import seed_database
from app.trading_agents import readiness
from app.trading_agents.runtime import seed_trading_agents_runtime
from app.trading_agents.runtime import find_matching_dry_run
from app.trading_agents.config import configuration_fingerprint, openai_base_url


def test_trading_agents_models_and_seed_are_simulation_only(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'agents.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=str(engine.url)))
        seed_trading_agents_runtime(db, Settings(database_url=str(engine.url)))

        definition = db.scalar(
            select(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        account = db.scalar(
            select(SimulationAccount).where(
                SimulationAccount.name == "TradingAgents 模拟账户"
            )
        )
        config = db.scalar(
            select(StrategyConfig).where(
                StrategyConfig.strategy_definition_id == definition.id
            )
        )
        schedules = list(
            db.scalars(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id == config.id
                )
            )
        )

        assert definition.enabled and definition.market == "A_SHARE"
        assert account.initial_cash == 100_000
        assert config.mode == "SIMULATION"
        assert config.simulation_account_id == account.id
        assert config.parameters["analysis_profile"] == "a_share_balanced"
        assert config.parameters["quick_model"] == "gpt-5.6-terra"
        assert config.parameters["deep_model"] == "gpt-5.6-sol"
        assert {item.trigger_type for item in schedules} == {
            "agent_analysis",
            "agent_rebalance",
        }
        assert all(not item.enabled for item in schedules)

        assert MarketDailyBar.__tablename__ in Base.metadata.tables
        assert TradingAgentBatch.__tablename__ in Base.metadata.tables
        assert TradingAgentCandidateAnalysis.__tablename__ in Base.metadata.tables
        assert TradingAgentPortfolioDecision.__tablename__ in Base.metadata.tables


def test_readiness_fails_closed_without_openai_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = readiness(Settings())
    assert result["ready"] is False
    assert result["openai_configured"] is False
    assert "OPENAI_API_KEY" in result["reasons"]
    assert "openai_api_key" not in result


def test_openai_base_url_prefers_primary_name_and_supports_legacy_alias(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://primary.example/v1")
    monkeypatch.setenv("OPENAI_API_BASE", "https://legacy.example/v1")
    assert openai_base_url() == "https://primary.example/v1"

    monkeypatch.delenv("OPENAI_BASE_URL")
    assert openai_base_url() == "https://legacy.example/v1"


def test_readiness_reports_endpoint_boolean_without_exposing_url(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://secret-endpoint.example/v1")

    result = readiness(Settings())

    assert result["custom_endpoint_configured"] is True
    assert "openai_base_url" not in result
    assert "https://secret-endpoint.example/v1" not in str(result)


def test_dry_run_must_match_current_configuration(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'dry-run.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_trading_agents_runtime(db, settings)
        fingerprint = configuration_fingerprint(
            config.parameters,
            simulation_account_id=config.simulation_account_id,
        )
        db.add(
            TradingAgentBatch(
                strategy_config_id=config.id,
                simulation_account_id=config.simulation_account_id,
                trading_date="2026-07-14",
                status="dry_run_completed",
                analysis_profile="a_share_balanced",
                position_mapping="fixed_rating",
                quick_model="gpt-5.4-mini",
                deep_model="gpt-5.2",
                prompt_version="1",
                config_fingerprint=fingerprint,
                candidate_symbols=[],
                holding_symbols=[],
                required_symbols=[],
                analysis_deadline=datetime(2026, 7, 14, 14, 42),
                rebalance_after=datetime(2026, 7, 14, 14, 45),
            )
        )
        db.commit()

        assert find_matching_dry_run(db, config) is not None
        config.parameters = {**config.parameters, "deep_model": "gpt-5.4"}
        db.commit()
        assert find_matching_dry_run(db, config) is None


def test_runtime_restart_disables_agent_schedules_when_readiness_is_lost(
    tmp_path,
    monkeypatch,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'restart-gate.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
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
        db.commit()

        seed_trading_agents_runtime(db, settings)
        db.expire_all()
        schedules = list(
            db.scalars(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id == config.id
                )
            )
        )

    assert all(not schedule.enabled for schedule in schedules)


def test_runtime_restart_preserves_admin_selected_active_simulation_account(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'selected-account.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_trading_agents_runtime(db, settings)
        selected = SimulationAccount(
            name="管理员选择账户",
            initial_cash=200_000,
            cash_balance=200_000,
            available_cash=200_000,
            total_asset=200_000,
            commission_rate=0.0003,
            min_commission=5,
            stamp_tax_rate=0.0005,
            transfer_fee_rate=0,
            slippage_bps=5,
        )
        db.add(selected)
        db.flush()
        db.add(
            SimulationAccountLedger(
                simulation_account_id=selected.id,
                event_type="initialize",
                amount=200_000,
                balance_after=200_000,
                message="测试账户",
            )
        )
        config.simulation_account_id = selected.id
        db.commit()
        selected_id = selected.id

        restarted = seed_trading_agents_runtime(db, settings)
        restarted_account_id = restarted.simulation_account_id

    assert restarted_account_id == selected_id


def test_readiness_requires_exact_fixed_upstream_commit(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setattr("app.trading_agents.config.importlib.util.find_spec", lambda _: object())
    monkeypatch.setattr("app.trading_agents.config.metadata.version", lambda _: "0.3.1")
    monkeypatch.setattr(
        "app.trading_agents.config.metadata.distribution",
        lambda _: SimpleNamespace(
            read_text=lambda _: (
                '{"vcs_info":{"commit_id":"ffffffffffffffffffffffffffffffffffffffff"}}'
            )
        ),
    )

    result = readiness(Settings(live_enabled=False, broker_adapter="simulation"))

    assert result["ready"] is False
    assert result["dependency_commit_valid"] is False
    assert "tradingagents_commit" in result["reasons"]


def test_runtime_restart_replaces_a_manually_shared_account(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shared-account.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        agent_config = seed_trading_agents_runtime(db, settings)
        dedicated_id = agent_config.simulation_account_id
        shared = SimulationAccount(
            name="手工共享账户",
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
        overnight = db.scalar(
            select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold")
        )
        db.add(
            StrategyConfig(
                strategy_definition_id=overnight.id,
                name="占用者",
                mode="SIMULATION",
                parameters={},
                simulation_account_id=shared.id,
            )
        )
        agent_config.simulation_account_id = shared.id
        db.commit()

        restarted = seed_trading_agents_runtime(db, settings)
        restarted_account_id = restarted.simulation_account_id

    assert restarted_account_id == dedicated_id
