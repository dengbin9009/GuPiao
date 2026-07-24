from __future__ import annotations

import time
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .config import Settings
from .database import SessionLocal
from .models import Administrator
from .probability_portfolio.runtime import seed_probability_portfolio_runtime
from .quant_strategies.runtime import seed_quant_strategy_runtimes
from .trading_agents.runtime import seed_trading_agents_runtime


def seed_strategy_runtimes(db: Session, settings: Settings) -> dict[str, object]:
    return {
        "trading_agents": seed_trading_agents_runtime(db, settings),
        "probability_portfolio": seed_probability_portfolio_runtime(db, settings),
        "quant_strategies": seed_quant_strategy_runtimes(db, settings),
    }


def wait_for_runtime_database(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = 2,
) -> None:
    """Wait until the API owner has created and seeded the shared database."""
    while True:
        try:
            with session_factory() as db:
                ready = db.scalar(select(Administrator.id).limit(1)) is not None
            if ready:
                return
        except SQLAlchemyError:
            pass
        sleep(poll_seconds)
