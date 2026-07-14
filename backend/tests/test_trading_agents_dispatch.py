from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import StrategyConfig, StrategyDefinition, TradingAgentBatch
from app.services import seed_database
from app.strategy_execution import execute_strategy_trigger
from app.scheduler_runner import schedule_tolerance_seconds
from app.trading_agents.runtime import seed_trading_agents_runtime


SHANGHAI = ZoneInfo("Asia/Shanghai")


def setup_dispatch_db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'dispatch.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=str(engine.url)))
        seed_trading_agents_runtime(db, Settings(database_url=str(engine.url)))
    return engine


def test_agent_analysis_trigger_creates_async_batch_run(tmp_path, monkeypatch):
    engine = setup_dispatch_db(tmp_path)
    current = datetime(2026, 7, 14, 13, 30, tzinfo=SHANGHAI)
    fake_batch = SimpleNamespace(id=9, status="pending")
    monkeypatch.setattr("app.strategy_execution.create_batch", lambda *a, **k: fake_batch)

    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        run = execute_strategy_trigger(
            db,
            config,
            "agent_analysis",
            current=current,
        )
        run_status = run.status
        run_summary = dict(run.summary)

    assert run_status == "completed"
    assert run_summary == {"accepted": 1, "batch_id": 9, "batch_status": "pending"}


def test_agent_rebalance_retries_while_batch_is_processing(tmp_path):
    engine = setup_dispatch_db(tmp_path)
    current = datetime(2026, 7, 14, 14, 46, tzinfo=SHANGHAI)
    with Session(engine) as db:
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        db.add(
            TradingAgentBatch(
                strategy_config_id=config.id,
                simulation_account_id=config.simulation_account_id,
                trading_date=current.date().isoformat(),
                status="processing",
                analysis_profile="a_share_balanced",
                position_mapping="fixed_rating",
                quick_model="gpt-5.4-mini",
                deep_model="gpt-5.2",
                candidate_symbols=[],
                holding_symbols=[],
                required_symbols=[],
                analysis_deadline=current - timedelta(minutes=4),
                rebalance_after=current - timedelta(minutes=1),
            )
        )
        db.commit()
        run = execute_strategy_trigger(
            db,
            config,
            "agent_rebalance",
            current=current,
        )
        run_status = run.status
        run_summary = dict(run.summary)

    assert run_status == "completed"
    assert run_summary["accepted"] == 0
    assert run_summary["retryable"] is True


def test_unknown_trigger_fails_closed(tmp_path):
    engine = setup_dispatch_db(tmp_path)
    with Session(engine) as db:
        config = db.scalar(select(StrategyConfig).limit(1))
        try:
            execute_strategy_trigger(db, config, "unknown")
        except ValueError as exc:
            assert "不支持" in str(exc)
        else:
            raise AssertionError("unknown trigger must fail")


def test_trading_agents_worker_claims_and_processes_one_batch(monkeypatch):
    from app.trading_agents import worker

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    session = FakeSession()
    claimed = SimpleNamespace(id=3)
    processed = SimpleNamespace(id=3, status="ready")
    monkeypatch.setattr(worker, "claim_pending_batch", lambda *a, **k: claimed)
    monkeypatch.setattr(worker, "process_batch", lambda *a, **k: processed)

    result = worker.process_one(
        session_factory=lambda: session,
        analyzer=SimpleNamespace(),
        worker_id="worker-test",
    )

    assert result == {"claimed": 1, "batch_id": 3, "status": "ready"}


def test_agent_analysis_schedule_can_recover_until_analysis_deadline():
    config = SimpleNamespace(parameters={"analysis_deadline": "14:42"})
    schedule = SimpleNamespace(trigger_type="agent_analysis", run_time="13:30")

    assert schedule_tolerance_seconds(schedule, config) == 72 * 60
