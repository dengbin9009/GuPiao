from __future__ import annotations


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
