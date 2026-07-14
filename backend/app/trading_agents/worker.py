from __future__ import annotations

import logging
import os
import socket
import time
from typing import Any, Callable

from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import Base, SessionLocal, apply_runtime_migrations, engine
from ..services import seed_database
from .adapter import TradingAgentsAnalyzer
from .batches import claim_pending_batch, process_batch
from .runtime import seed_trading_agents_runtime


LOGGER = logging.getLogger("gupiao.tradingagents-worker")


def process_one(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    analyzer: Any | None = None,
    worker_id: str | None = None,
) -> dict[str, Any]:
    analyzer = analyzer or TradingAgentsAnalyzer()
    worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    with session_factory() as db:
        batch = claim_pending_batch(db, worker_id=worker_id)
        if batch is None:
            return {"claimed": 0}
        processed = process_batch(db, batch, analyzer=analyzer)
        return {
            "claimed": 1,
            "batch_id": processed.id,
            "status": processed.status,
        }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    Base.metadata.create_all(bind=engine)
    apply_runtime_migrations()
    with SessionLocal() as db:
        seed_database(db, get_settings())
        seed_trading_agents_runtime(db, get_settings())
    LOGGER.info("TradingAgents 独立 Worker 已启动")
    while True:
        try:
            result = process_one()
            if result["claimed"]:
                LOGGER.info(
                    "TradingAgents 批次处理完成 batch_id=%s status=%s",
                    result["batch_id"],
                    result["status"],
                )
        except Exception:
            LOGGER.exception("TradingAgents Worker 迭代失败，将在下一轮继续")
        time.sleep(2)


if __name__ == "__main__":
    main()
