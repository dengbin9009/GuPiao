from __future__ import annotations

from pathlib import Path


def test_sqlite_runtime_engine_uses_wal_and_busy_timeout(tmp_path):
    from app.database import create_database_engine

    engine = create_database_engine(f"sqlite:///{tmp_path / 'runtime.db'}")

    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()

    assert journal_mode.lower() == "wal"
    assert busy_timeout == 30_000


def test_runtime_migration_adds_schedule_retry_column(tmp_path):
    from sqlalchemy import text

    from app.database import apply_runtime_migrations, create_database_engine

    database_url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE strategy_schedules ("
                "id INTEGER PRIMARY KEY, "
                "strategy_config_id INTEGER, "
                "trigger_type VARCHAR(32), "
                "enabled BOOLEAN, "
                "run_time VARCHAR(16), "
                "last_scheduled_for VARCHAR(64)"
                ")"
            )
        )

    apply_runtime_migrations(database_engine=engine, database_url=database_url)

    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(strategy_schedules)"
            )
        }
    assert "next_run_at" in columns


def test_runtime_migration_adds_strategy_simulation_account_binding(tmp_path):
    from sqlalchemy import text

    from app.database import apply_runtime_migrations, create_database_engine

    database_url = f"sqlite:///{tmp_path / 'legacy-strategy.db'}"
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE strategy_configs ("
                "id INTEGER PRIMARY KEY, "
                "strategy_definition_id INTEGER, "
                "name VARCHAR(128), "
                "mode VARCHAR(16), "
                "parameters JSON, "
                "enabled BOOLEAN"
                ")"
            )
        )

    apply_runtime_migrations(database_engine=engine, database_url=database_url)

    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(strategy_configs)"
            )
        }
    assert "simulation_account_id" in columns


def test_runtime_migration_adds_probability_factor_columns_to_stocks(tmp_path):
    from sqlalchemy import text

    from app.database import apply_runtime_migrations, create_database_engine

    database_url = f"sqlite:///{tmp_path / 'legacy-stocks.db'}"
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE stocks ("
                "id INTEGER PRIMARY KEY, "
                "symbol VARCHAR(24), "
                "last_price FLOAT, "
                "quote_updated_at DATETIME"
                ")"
            )
        )

    apply_runtime_migrations(database_engine=engine, database_url=database_url)

    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(stocks)")
        }
    assert {
        "listing_date",
        "float_shares",
        "turnover_rate",
        "open_price",
        "high_price",
        "low_price",
        "volume",
        "vwap",
        "tail_30m_return",
        "limit_up_price",
        "limit_down_price",
        "quote_source",
        "factor_updated_at",
    } <= columns


def test_runtime_migration_adds_probability_run_config_fingerprint(tmp_path):
    from sqlalchemy import text

    from app.database import apply_runtime_migrations, create_database_engine

    database_url = f"sqlite:///{tmp_path / 'legacy-probability-runs.db'}"
    engine = create_database_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE probability_portfolio_runs ("
                "id INTEGER PRIMARY KEY, "
                "strategy_config_id INTEGER, "
                "trading_date VARCHAR(10), "
                "trigger_type VARCHAR(32)"
                ")"
            )
        )

    apply_runtime_migrations(database_engine=engine, database_url=database_url)

    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.exec_driver_sql(
                "PRAGMA table_info(probability_portfolio_runs)"
            )
        }
    assert "config_fingerprint" in columns


def test_probability_portfolio_mysql_migration_contains_all_new_tables_and_columns():
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "0003_probability_portfolio.sql"
    ).read_text(encoding="utf-8")

    for table in (
        "probability_model_artifacts",
        "probability_training_samples",
        "probability_portfolio_runs",
        "probability_candidate_decisions",
        "strategy_position_lots",
    ):
        assert f"CREATE TABLE {table}" in migration
    for column in (
        "listing_date",
        "float_shares",
        "turnover_rate",
        "vwap",
        "tail_30m_return",
        "factor_updated_at",
    ):
        assert f"ADD COLUMN {column}" in migration
    assert "config_fingerprint VARCHAR(64)" in migration
