from __future__ import annotations

from sqlalchemy.orm import Session

from .config import Settings
from .probability_portfolio.runtime import seed_probability_portfolio_runtime
from .trading_agents.runtime import seed_trading_agents_runtime


def seed_strategy_runtimes(db: Session, settings: Settings) -> dict[str, object]:
    return {
        "trading_agents": seed_trading_agents_runtime(db, settings),
        "probability_portfolio": seed_probability_portfolio_runtime(db, settings),
    }
