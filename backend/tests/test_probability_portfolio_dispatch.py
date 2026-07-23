from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.probability_portfolio.candidates import CandidateBuildResult
from app.probability_portfolio.execution import RejectedCandidate
from app.probability_portfolio.runtime import seed_probability_portfolio_runtime
from app.services import seed_database
from app.strategy_execution import execute_portfolio_entry_trigger, execute_strategy_trigger


def test_probability_strategy_dispatches_entry_and_exit(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'dispatch.db'}")
    Base.metadata.create_all(engine)
    current = datetime(2026, 7, 23, 14, 40, tzinfo=ZoneInfo("Asia/Shanghai"))
    calls = []

    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)

        def entry(_db, _config, *, current):
            calls.append(("entry", current))
            return "entry-run"

        def exit_(_db, _config, *, current):
            calls.append(("exit", current))
            return "exit-run"

        monkeypatch.setattr("app.strategy_execution.execute_portfolio_entry_trigger", entry)
        monkeypatch.setattr("app.strategy_execution.execute_portfolio_exit", exit_)

        assert execute_strategy_trigger(db, config, "portfolio_entry", current=current) == "entry-run"
        assert execute_strategy_trigger(db, config, "portfolio_exit", current=current) == "exit-run"

    assert calls == [("entry", current), ("exit", current)]


def test_probability_entry_retries_when_snapshot_data_is_still_pending(
    tmp_path,
    monkeypatch,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'pending.db'}")
    Base.metadata.create_all(engine)
    current = datetime(2026, 7, 23, 14, 40, tzinfo=ZoneInfo("Asia/Shanghai"))
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        monkeypatch.setattr(
            "app.strategy_execution.build_scored_candidates",
            lambda *args, **kwargs: CandidateBuildResult(
                [],
                [RejectedCandidate(1, "000001.SZ", ("行情或因子时间缺失",))],
                (),
                1,
            ),
        )

        with pytest.raises(RuntimeError, match="仍在准备"):
            execute_portfolio_entry_trigger(db, config, current=current)


def test_probability_entry_completes_zero_trade_when_only_strategy_filters_reject(
    tmp_path,
    monkeypatch,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'filtered.db'}")
    Base.metadata.create_all(engine)
    current = datetime(2026, 7, 23, 14, 40, tzinfo=ZoneInfo("Asia/Shanghai"))
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        monkeypatch.setattr(
            "app.strategy_execution.build_scored_candidates",
            lambda *args, **kwargs: CandidateBuildResult(
                [],
                [RejectedCandidate(1, "000001.SZ", ("日内涨幅不在1%至5%范围",))],
                (),
                1,
            ),
        )

        run = execute_portfolio_entry_trigger(db, config, current=current)

        assert run.summary["accepted"] == 0
        assert run.summary["data_quality_rejected"] == 1
