from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import (
    FinancialReportSnapshot,
    MarketDailyMetric,
    QuantCandidateScore,
    QuantPortfolioDecision,
    QuantStrategyTask,
    SimulationAccount,
    StrategyBacktestQualification,
    StrategyConfig,
    StrategyDefinition,
    StrategyDryRunApproval,
    StrategyPerformanceDaily,
    StrategyRiskProfile,
    StrategySchedule,
)
from app.quant_strategies.catalog import QUANT_STRATEGY_SPECS
from app.quant_strategies.runtime import INITIAL_CASH, seed_quant_strategy_runtimes
from app.services import seed_database


EXPECTED_KEYS = {
    "multi_factor_core",
    "relative_strength_rotation",
    "breakout_trend",
    "short_term_reversal_t1",
    "low_vol_quality",
    "earnings_drift",
    "regime_allocator",
    "risk_parity_overlay",
}


def test_quant_strategy_catalog_defines_eight_simulation_strategies():
    assert set(QUANT_STRATEGY_SPECS) == EXPECTED_KEYS
    assert [spec.signal_time for spec in QUANT_STRATEGY_SPECS.values()] == [
        "16:30",
        "16:31",
        "16:32",
        "16:33",
        "16:34",
        "16:35",
        "16:36",
        "16:37",
    ]
    assert [spec.execution_time for spec in QUANT_STRATEGY_SPECS.values()] == [
        "09:35",
        "09:36",
        "09:37",
        "09:38",
        "09:39",
        "09:40",
        "09:41",
        "09:42",
    ]
    assert all(spec.simulation_only for spec in QUANT_STRATEGY_SPECS.values())
    assert {
        key: spec.version for key, spec in QUANT_STRATEGY_SPECS.items()
    } == {
        "multi_factor_core": "1.0.1",
        "relative_strength_rotation": "1.0.1",
        "breakout_trend": "1.0.1",
        "short_term_reversal_t1": "1.0.1",
        "low_vol_quality": "1.0.1",
        "earnings_drift": "1.0.1",
        "regime_allocator": "1.0.0",
        "risk_parity_overlay": "1.0.0",
    }


def test_quant_runtime_seeds_independent_accounts_and_disabled_schedules(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'quant-runtime.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(
        database_url=database_url,
        live_enabled=False,
        broker_adapter="simulation",
    )

    with Session(engine) as db:
        seed_database(db, settings)
        first = seed_quant_strategy_runtimes(db, settings)
        accounts_before = {
            config.id: db.get(SimulationAccount, config.simulation_account_id).cash_balance
            for config in first.values()
        }
        db.get(SimulationAccount, first["multi_factor_core"].simulation_account_id).cash_balance -= 123
        db.commit()
        second = seed_quant_strategy_runtimes(db, settings)

        definitions = list(
            db.scalars(select(StrategyDefinition).where(StrategyDefinition.key.in_(EXPECTED_KEYS)))
        )
        configs = list(
            db.scalars(
                select(StrategyConfig).where(
                    StrategyConfig.strategy_definition_id.in_([item.id for item in definitions])
                )
            )
        )
        schedules = list(
            db.scalars(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id.in_([item.id for item in configs])
                )
            )
        )
        risks = list(
            db.scalars(
                select(StrategyRiskProfile).where(
                    StrategyRiskProfile.strategy_config_id.in_([item.id for item in configs])
                )
            )
        )

        assert set(first) == EXPECTED_KEYS
        assert {key: value.id for key, value in first.items()} == {
            key: value.id for key, value in second.items()
        }
        assert len(definitions) == 8
        assert len(configs) == 8
        assert len({item.simulation_account_id for item in configs}) == 8
        assert all(item.mode == "SIMULATION" for item in configs)
        assert all(db.get(SimulationAccount, item.simulation_account_id).initial_cash == INITIAL_CASH for item in configs)
        assert len(schedules) == 16
        assert all(not item.enabled for item in schedules)
        assert {item.trigger_type for item in schedules} == {"quant_signal", "quant_execute"}
        assert len(risks) == 8
        assert all(item.daily_loss_limit_pct == 0.02 for item in risks)
        assert all(item.max_drawdown_pct == 0.15 for item in risks)
        assert all(item.max_consecutive_errors == 3 for item in risks)
        assert accounts_before[first["multi_factor_core"].id] - 123 == db.get(
            SimulationAccount,
            second["multi_factor_core"].simulation_account_id,
        ).cash_balance


def test_quant_runtime_repairs_only_a_missing_account_binding(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'quant-runtime-repair.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(
        database_url=database_url,
        live_enabled=False,
        broker_adapter="simulation",
    )

    with Session(engine) as db:
        seed_database(db, settings)
        first = seed_quant_strategy_runtimes(db, settings)
        broken = first["multi_factor_core"]
        preserved = first["relative_strength_rotation"]
        preserved_account = db.get(
            SimulationAccount,
            preserved.simulation_account_id,
        )
        preserved_account.cash_balance -= 321
        broken.simulation_account_id = None
        db.commit()

        repaired = seed_quant_strategy_runtimes(db, settings)

        repaired_account = db.get(
            SimulationAccount,
            repaired["multi_factor_core"].simulation_account_id,
        )
        assert repaired_account is not None
        assert repaired_account.initial_cash == INITIAL_CASH
        assert repaired_account.cash_balance == INITIAL_CASH
        assert repaired["relative_strength_rotation"].simulation_account_id == preserved_account.id
        assert db.get(SimulationAccount, preserved_account.id).cash_balance == INITIAL_CASH - 321
        assert len(
            {
                config.simulation_account_id
                for config in repaired.values()
            }
        ) == 8


def test_quant_runtime_updates_existing_definition_versions_without_resetting_state(
    tmp_path: Path,
):
    database_url = f"sqlite:///{tmp_path / 'quant-version-refresh.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(
        database_url=database_url,
        live_enabled=False,
        broker_adapter="simulation",
    )
    with Session(engine) as db:
        seed_database(db, settings)
        configs = seed_quant_strategy_runtimes(db, settings)
        config = configs["breakout_trend"]
        definition = db.get(StrategyDefinition, config.strategy_definition_id)
        account = db.get(SimulationAccount, config.simulation_account_id)
        account.cash_balance -= 456
        definition.version = "0.9.0"
        db.commit()

        refreshed = seed_quant_strategy_runtimes(db, settings)["breakout_trend"]

        db.refresh(definition)
        db.refresh(account)
        assert definition.version == "1.0.1"
        assert refreshed.id == config.id
        assert refreshed.simulation_account_id == account.id
        assert account.cash_balance == INITIAL_CASH - 456


def test_quant_audit_models_are_registered():
    table_names = set(Base.metadata.tables)
    assert {
        MarketDailyMetric.__tablename__,
        FinancialReportSnapshot.__tablename__,
        QuantStrategyTask.__tablename__,
        QuantPortfolioDecision.__tablename__,
        QuantCandidateScore.__tablename__,
        StrategyRiskProfile.__tablename__,
        StrategyPerformanceDaily.__tablename__,
        StrategyBacktestQualification.__tablename__,
        StrategyDryRunApproval.__tablename__,
    } <= table_names


def test_mysql_migration_contains_quant_strategy_tables_and_security_defaults():
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "0004_independent_strategy_suite.sql"
    ).read_text(encoding="utf-8")

    for table in (
        "market_daily_metrics",
        "financial_report_snapshots",
        "quant_strategy_tasks",
        "quant_portfolio_decisions",
        "quant_candidate_scores",
        "strategy_risk_profiles",
        "strategy_performance_daily",
        "strategy_backtest_qualifications",
        "strategy_dry_run_approvals",
    ):
        assert f"CREATE TABLE {table}" in migration
    assert "ADD COLUMN instrument_type" in migration
    assert "ADD COLUMN lot_size" in migration
    assert "ADD COLUMN settlement_days" in migration
    assert "ADD COLUMN metadata" in migration
    assert "snapshot JSON" in migration
    assert "DEFAULT FALSE" in migration
    assert (
        "strategy_config_id, trading_date, decision_type, config_fingerprint"
        in migration
    )


def test_quant_runtime_does_not_create_live_orders(tmp_path: Path):
    from app.models import Order

    database_url = f"sqlite:///{tmp_path / 'quant-safety.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(
            database_url=database_url,
            live_enabled=False,
            broker_adapter="simulation",
        )
        seed_database(db, settings)
        seed_quant_strategy_runtimes(db, settings)
        assert db.scalar(select(func.count()).select_from(Order).where(Order.mode == "LIVE")) == 0


def test_seeded_data_source_capabilities_describe_quant_datasets(tmp_path: Path):
    from app.models import DataSourceState

    database_url = f"sqlite:///{tmp_path / 'sources.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_database(
            db,
            Settings(
                database_url=database_url,
                live_enabled=False,
                broker_adapter="simulation",
            ),
        )
        tushare = db.scalar(
            select(DataSourceState).where(DataSourceState.provider == "tushare")
        )
        akshare = db.scalar(
            select(DataSourceState).where(DataSourceState.provider == "akshare")
        )

        assert {
            "adjustment",
            "daily_metric",
            "financial",
            "etf_master",
            "etf_daily",
        } <= set(tushare.capabilities)
        assert {"etf_master", "etf_daily"} <= set(akshare.capabilities)


def test_seed_database_refreshes_capability_metadata_for_existing_sources(
    tmp_path: Path,
):
    from app.models import DataSourceState

    database_url = f"sqlite:///{tmp_path / 'source-refresh.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(
        database_url=database_url,
        live_enabled=False,
        broker_adapter="simulation",
    )
    with Session(engine) as db:
        seed_database(db, settings)
        tushare = db.scalar(
            select(DataSourceState).where(DataSourceState.provider == "tushare")
        )
        tushare.capabilities = ["daily"]
        db.commit()

        seed_database(db, settings)
        db.refresh(tushare)

        assert {
            "daily",
            "adjustment",
            "daily_metric",
            "financial",
            "etf_master",
            "etf_daily",
        } <= set(tushare.capabilities)
