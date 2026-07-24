from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database import Base
from app.models import (
    QuantPortfolioDecision,
    QuantStrategyTask,
    StrategyConfig,
    StrategyDefinition,
)
from app.quant_strategies.runtime import seed_quant_strategy_runtimes
from app.quant_strategies.readiness import configuration_fingerprint
from app.services import seed_database
from app.strategy_execution import execute_strategy_trigger


SHANGHAI = ZoneInfo("Asia/Shanghai")


def setup_db(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'dispatch.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(database_url=database_url, live_enabled=False, broker_adapter="simulation")
    with Session(engine) as db:
        seed_database(db, settings)
        configs = seed_quant_strategy_runtimes(db, settings)
        config_id = configs["multi_factor_core"].id
    return engine, config_id


def test_strategy_trigger_enqueues_quant_signal_and_execution_tasks(tmp_path: Path):
    engine, config_id = setup_db(tmp_path)
    signal_time = datetime(2026, 7, 31, 16, 30, tzinfo=SHANGHAI)
    execute_time = datetime(2026, 8, 3, 9, 35, tzinfo=SHANGHAI)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_id)
        signal_run = execute_strategy_trigger(db, config, "quant_signal", current=signal_time)
        definition = db.get(StrategyDefinition, config.strategy_definition_id)
        decision = QuantPortfolioDecision(
            strategy_config_id=config.id,
            simulation_account_id=config.simulation_account_id,
            trading_date=signal_time.date().isoformat(),
            decision_type="signal",
            status="ready",
            data_as_of=signal_time,
            config_fingerprint=configuration_fingerprint(
                config.parameters,
                simulation_account_id=config.simulation_account_id,
                strategy_version=definition.version,
            ),
            strategy_version=definition.version,
            data_version="1",
            target_weights={"000001.SZ": 0.1},
        )
        db.add(decision)
        db.commit()
        execution_run = execute_strategy_trigger(db, config, "quant_execute", current=execute_time)
        tasks = list(db.scalars(select(QuantStrategyTask).order_by(QuantStrategyTask.id)))

        assert signal_run.summary["queued"] is True
        assert execution_run.summary["queued"] is True
        assert [item.task_type for item in tasks] == ["signal", "execute"]
        assert [item.trading_date for item in tasks] == ["2026-07-31", "2026-08-03"]
        assert tasks[1].payload == {
            "decision_id": decision.id,
            "expected_signal_date": "2026-07-31",
            "next_sellable_date": "2026-08-04",
        }
        assert tasks[1].idempotency_key.endswith(f":execute:decision-{decision.id}")
        assert tasks[1].deadline_at.hour == 10
        assert tasks[1].deadline_at.minute == 0
        assert tasks[1].max_attempts > 3
        assert tasks[0].deadline_at.hour == 23
        assert tasks[0].max_attempts >= 100


def test_quant_execution_without_previous_trading_day_decision_is_a_clean_skip(
    tmp_path: Path,
):
    engine, config_id = setup_db(tmp_path)
    current = datetime(2026, 7, 27, 9, 35, tzinfo=SHANGHAI)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_id)

        run = execute_strategy_trigger(db, config, "quant_execute", current=current)

        assert run.summary["accepted"] == 0
        assert run.summary["queued"] is False
        assert "待执行" in run.summary["reason"]
        assert list(db.scalars(select(QuantStrategyTask))) == []


def test_quant_execution_rejects_an_older_ready_decision(tmp_path: Path):
    engine, config_id = setup_db(tmp_path)
    current = datetime(2026, 7, 27, 9, 35, tzinfo=SHANGHAI)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_id)
        definition = db.get(StrategyDefinition, config.strategy_definition_id)
        db.add(
            QuantPortfolioDecision(
                strategy_config_id=config.id,
                simulation_account_id=config.simulation_account_id,
                trading_date="2026-07-23",
                decision_type="signal",
                status="ready",
                data_as_of=current - timedelta(days=4),
                config_fingerprint=configuration_fingerprint(
                    config.parameters,
                    simulation_account_id=config.simulation_account_id,
                    strategy_version=definition.version,
                ),
                strategy_version=definition.version,
                data_version="1",
                target_weights={"000001.SZ": 0.10},
            )
        )
        db.commit()

        run = execute_strategy_trigger(
            db,
            config,
            "quant_execute",
            current=current,
        )

        assert run.summary["queued"] is False
        assert "上一交易日" in run.summary["reason"]
        assert list(db.scalars(select(QuantStrategyTask))) == []


def test_quant_signal_frequency_uses_exchange_holiday_calendar(tmp_path: Path):
    engine, config_id = setup_db(tmp_path)
    current = datetime(2026, 9, 29, 16, 30, tzinfo=SHANGHAI)
    next_open = date(2026, 10, 9)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_id)
        run = execute_strategy_trigger(
            db,
            config,
            "quant_signal",
            current=current,
            trading_day_fn=lambda day: day == next_open,
        )

        assert run.summary["queued"] is True


def test_quant_worker_processes_signal_task_and_keeps_other_tasks_independent(
    tmp_path: Path,
    monkeypatch,
):
    from app.quant_strategies import worker

    engine, config_id = setup_db(tmp_path)
    factory = sessionmaker(bind=engine)
    current = datetime(2026, 7, 31, 16, 31, tzinfo=SHANGHAI)
    with factory() as db:
        config = db.get(StrategyConfig, config_id)
        execute_strategy_trigger(db, config, "quant_signal", current=current)
    fake_decision = SimpleNamespace(id=17, status="ready", target_weights={"000001.SZ": 0.1})
    monkeypatch.setattr(worker, "build_signal_decision", lambda *args, **kwargs: fake_decision)

    result = worker.process_one(
        session_factory=factory,
        worker_id="test-worker",
        current=current,
    )

    with factory() as db:
        task = db.scalar(select(QuantStrategyTask))
        assert result == {"claimed": 1, "task_id": task.id, "status": "completed"}
        assert task.result["decision_id"] == 17


def test_quant_worker_defines_one_realtime_lane_per_strategy_and_separate_backtests(
    tmp_path: Path,
):
    from app.quant_strategies.worker import worker_lane_specs

    engine, _config_id = setup_db(tmp_path)
    with Session(engine) as db:
        lanes = worker_lane_specs(db)

    realtime = [item for item in lanes if item.task_types == frozenset({"signal", "execute"})]
    backtests = [item for item in lanes if item.task_types == frozenset({"backtest"})]
    assert len(realtime) == 8
    assert len({item.strategy_config_id for item in realtime}) == 8
    assert len(backtests) == 2
    assert all(item.strategy_config_id is None for item in backtests)


def test_quant_worker_refuses_execution_when_readiness_gate_is_closed(
    tmp_path: Path,
):
    from app.quant_strategies import worker

    engine, config_id = setup_db(tmp_path)
    factory = sessionmaker(bind=engine)
    current = datetime(2026, 7, 27, 9, 35, tzinfo=SHANGHAI)
    with factory() as db:
        config = db.get(StrategyConfig, config_id)
        definition = db.get(StrategyDefinition, config.strategy_definition_id)
        db.add(
            QuantPortfolioDecision(
                strategy_config_id=config.id,
                simulation_account_id=config.simulation_account_id,
                trading_date="2026-07-24",
                decision_type="signal",
                status="ready",
                data_as_of=current - timedelta(days=3),
                config_fingerprint="closed-gate",
                strategy_version=definition.version,
                data_version="1",
                target_weights={"000001.SZ": 0.10},
            )
        )
        db.commit()
        execute_strategy_trigger(db, config, "quant_execute", current=current)

    result = worker.process_one(
        session_factory=factory,
        worker_id="test-worker",
        current=current,
    )

    with factory() as db:
        task = db.scalar(select(QuantStrategyTask))
        assert result["status"] == "failed"
        assert task.status == "failed"
        assert "上线闸门" in task.error_message


def test_quant_worker_releases_retryable_decision_before_execution_deadline(
    tmp_path: Path,
    monkeypatch,
):
    from app.quant_strategies import worker

    engine, config_id = setup_db(tmp_path)
    factory = sessionmaker(bind=engine)
    current = datetime(2026, 7, 27, 9, 35, tzinfo=SHANGHAI)
    with factory() as db:
        config = db.get(StrategyConfig, config_id)
        definition = db.get(StrategyDefinition, config.strategy_definition_id)
        decision = QuantPortfolioDecision(
            strategy_config_id=config.id,
            simulation_account_id=config.simulation_account_id,
            trading_date="2026-07-24",
            decision_type="signal",
            status="ready",
            data_as_of=current - timedelta(days=3),
            config_fingerprint=configuration_fingerprint(
                config.parameters,
                simulation_account_id=config.simulation_account_id,
                strategy_version=definition.version,
            ),
            strategy_version=definition.version,
            data_version="1",
            target_weights={"000001.SZ": 0.10},
        )
        db.add(decision)
        db.commit()
        execute_strategy_trigger(db, config, "quant_execute", current=current)
        decision_id = decision.id

    monkeypatch.setattr(
        worker,
        "quant_strategy_readiness",
        lambda *args, **kwargs: {
            "automation_ready": True,
            "reasons": [],
        },
    )
    fake_run = SimpleNamespace(
        id=77,
        summary={
            "accepted": 0,
            "reason": "000001.SZ 行情已过期",
            "retryable": True,
        },
    )
    monkeypatch.setattr(worker, "execute_quant_rebalance", lambda *args, **kwargs: fake_run)

    result = worker.process_one(
        session_factory=factory,
        worker_id="test-worker",
        current=current,
    )

    with factory() as db:
        task = db.scalar(select(QuantStrategyTask))
        decision = db.get(QuantPortfolioDecision, decision_id)
        assert result["status"] == "retry"
        assert task.status == "retry"
        assert decision.status == "ready"
        assert decision.strategy_run_id is None


def test_quant_worker_rolls_back_partial_execution_before_recording_failure(
    tmp_path: Path,
    monkeypatch,
):
    from app.quant_strategies import worker

    engine, config_id = setup_db(tmp_path)
    factory = sessionmaker(bind=engine)
    current = datetime(2026, 7, 31, 16, 31, tzinfo=SHANGHAI)
    with factory() as db:
        task = QuantStrategyTask(
            strategy_config_id=config_id,
            simulation_account_id=db.get(StrategyConfig, config_id).simulation_account_id,
            task_type="signal",
            trading_date="2026-07-31",
            idempotency_key="partial-rollback",
            status="pending",
        )
        db.add(task)
        db.commit()

    def partial_write(db, *_args, **_kwargs):
        config = db.get(StrategyConfig, config_id)
        config.name = "不应提交的半成品"
        db.flush()
        raise RuntimeError("处理中断")

    monkeypatch.setattr(worker, "build_signal_decision", partial_write)

    result = worker.process_one(
        session_factory=factory,
        worker_id="test-worker",
        current=current,
    )

    with factory() as db:
        config = db.get(StrategyConfig, config_id)
        task = db.scalar(select(QuantStrategyTask))
        assert result["status"] == "retry"
        assert config.name == "多因子核心组合"
        assert task.status == "retry"


def test_quant_worker_rejects_backtest_task_after_configuration_changes(
    tmp_path: Path,
):
    from app.quant_strategies import worker

    engine, config_id = setup_db(tmp_path)
    factory = sessionmaker(bind=engine)
    current = datetime(2026, 7, 31, 17, 0, tzinfo=SHANGHAI)
    with factory() as db:
        config = db.get(StrategyConfig, config_id)
        definition = db.get(StrategyDefinition, config.strategy_definition_id)
        fingerprint = configuration_fingerprint(
            config.parameters,
            simulation_account_id=config.simulation_account_id,
            strategy_version=definition.version,
        )
        task = QuantStrategyTask(
            strategy_config_id=config.id,
            simulation_account_id=config.simulation_account_id,
            task_type="backtest",
            trading_date="2026-07-31",
            idempotency_key="stale-backtest-config",
            status="pending",
            payload={
                "start_date": "2023-01-01",
                "end_date": "2026-07-31",
                "config_fingerprint": fingerprint,
                "strategy_version": definition.version,
                "data_version": "1",
            },
        )
        db.add(task)
        config.parameters = {**config.parameters, "prefilter_size": 500}
        db.commit()

    result = worker.process_one(
        session_factory=factory,
        worker_id="test-worker",
        current=current,
    )

    with factory() as db:
        task = db.scalar(select(QuantStrategyTask))
        assert result["status"] == "failed"
        assert "配置已变化" in task.error_message
