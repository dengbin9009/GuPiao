from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def apply_runtime_migrations() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    with engine.begin() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(live_trading_accounts)")}
        if "market_permissions" not in columns:
            conn.execute(text("ALTER TABLE live_trading_accounts ADD COLUMN market_permissions JSON DEFAULT '[]'"))
        if "account_capabilities" not in columns:
            conn.execute(text("ALTER TABLE live_trading_accounts ADD COLUMN account_capabilities JSON DEFAULT '[]'"))
