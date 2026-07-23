from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def create_database_engine(database_url: str) -> Engine:
    if not database_url.startswith("sqlite"):
        return create_engine(database_url, pool_pre_ping=True)
    database_engine = create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(database_engine, "connect")
    def configure_sqlite(connection, _connection_record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return database_engine


settings = get_settings()
engine = create_database_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def apply_runtime_migrations(
    *,
    database_engine: Engine | None = None,
    database_url: str | None = None,
) -> None:
    target_engine = database_engine or engine
    target_url = database_url or str(target_engine.url)
    if not target_url.startswith("sqlite"):
        return
    with target_engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "live_trading_accounts" in tables:
            columns = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(live_trading_accounts)"
                )
            }
            if "market_permissions" not in columns:
                conn.execute(
                    text(
                        "ALTER TABLE live_trading_accounts "
                        "ADD COLUMN market_permissions JSON DEFAULT '[]'"
                    )
                )
            if "account_capabilities" not in columns:
                conn.execute(
                    text(
                        "ALTER TABLE live_trading_accounts "
                        "ADD COLUMN account_capabilities JSON DEFAULT '[]'"
                    )
                )
        if "strategy_schedules" in tables:
            schedule_columns = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(strategy_schedules)"
                )
            }
            if "next_run_at" not in schedule_columns:
                conn.execute(
                    text(
                        "ALTER TABLE strategy_schedules "
                        "ADD COLUMN next_run_at DATETIME"
                    )
                )
        if "strategy_configs" in tables:
            config_columns = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(strategy_configs)"
                )
            }
            if "simulation_account_id" not in config_columns:
                conn.execute(
                    text(
                        "ALTER TABLE strategy_configs "
                        "ADD COLUMN simulation_account_id INTEGER"
                    )
                )
        if "stocks" in tables:
            stock_columns = {
                row[1]
                for row in conn.exec_driver_sql("PRAGMA table_info(stocks)")
            }
            probability_factor_columns = {
                "listing_date": "VARCHAR(10)",
                "float_shares": "FLOAT",
                "turnover_rate": "FLOAT",
                "open_price": "FLOAT",
                "high_price": "FLOAT",
                "low_price": "FLOAT",
                "volume": "FLOAT",
                "vwap": "FLOAT",
                "tail_30m_return": "FLOAT",
                "limit_up_price": "FLOAT",
                "limit_down_price": "FLOAT",
                "quote_source": "VARCHAR(32)",
                "factor_updated_at": "DATETIME",
            }
            for column, column_type in probability_factor_columns.items():
                if column not in stock_columns:
                    conn.execute(
                        text(
                            f"ALTER TABLE stocks ADD COLUMN {column} {column_type}"
                        )
                    )
        if "trading_agent_batches" in tables:
            batch_columns = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(trading_agent_batches)"
                )
            }
            if "order_ids" not in batch_columns:
                conn.execute(
                    text(
                        "ALTER TABLE trading_agent_batches "
                        "ADD COLUMN order_ids JSON DEFAULT '[]'"
                    )
                )
            if "rebalance_run_id" not in batch_columns:
                conn.execute(
                    text(
                        "ALTER TABLE trading_agent_batches "
                        "ADD COLUMN rebalance_run_id INTEGER"
                    )
                )
            if "config_fingerprint" not in batch_columns:
                conn.execute(
                    text(
                        "ALTER TABLE trading_agent_batches "
                        "ADD COLUMN config_fingerprint VARCHAR(64)"
                    )
                )
        if "probability_portfolio_runs" in tables:
            probability_run_columns = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(probability_portfolio_runs)"
                )
            }
            if "config_fingerprint" not in probability_run_columns:
                conn.execute(
                    text(
                        "ALTER TABLE probability_portfolio_runs "
                        "ADD COLUMN config_fingerprint VARCHAR(64)"
                    )
                )
