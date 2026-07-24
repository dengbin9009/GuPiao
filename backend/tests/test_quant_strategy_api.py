from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.main import (
    QuantBacktestCreate,
    QuantStrategyConfigUpdate,
    ScheduleUpdate,
    activate_quant_strategy,
    create_quant_backtest,
    delete_schedule,
    get_quant_strategy,
    get_quant_strategy_readiness,
    list_quant_strategies,
    pause_quant_strategy,
    run_quant_strategy_dry_run,
    update_schedule,
    update_quant_strategy,
)
from app.models import (
    Order,
    NotificationChannel,
    NotificationDelivery,
    MarketDailyBar,
    Position,
    QuantCandidateScore,
    QuantPortfolioDecision,
    QuantStrategyTask,
    RiskEvent,
    Stock,
    StrategyBacktestQualification,
    StrategyConfig,
    StrategyRiskProfile,
    StrategyRun,
    StrategySchedule,
)
from app.quant_strategies.runtime import seed_quant_strategy_runtimes
from app.services import seed_database


CURRENT = datetime(2026, 7, 24, 16, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def setup_db(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'api.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    db = Session(engine)
    settings = Settings(database_url=database_url, live_enabled=False, broker_adapter="simulation")
    seed_database(db, settings)
    seed_quant_strategy_runtimes(db, settings)
    return db


def test_list_and_detail_return_eight_independent_strategies(tmp_path: Path):
    db = setup_db(tmp_path)
    try:
        rows = list_quant_strategies(None, db)
        detail = get_quant_strategy("multi_factor_core", None, db)

        assert len(rows) == 8
        assert len({item["simulation_account_id"] for item in rows}) == 8
        assert all(item["simulation_only"] for item in rows)
        assert detail["strategy_key"] == "multi_factor_core"
        assert detail["account"]["initial_cash"] == 2_000_000
        assert len(detail["schedules"]) == 2
        assert detail["risk"]["daily_loss_limit_pct"] == 0.02
        assert detail["positions"] == []
        assert detail["orders"] == []
        assert detail["performances"] == []
        assert detail["qualifications"] == []
        assert detail["latest_candidates"] == []
        assert detail["position_count"] == 0
        assert detail["consecutive_errors"] == 0
        assert set(detail["schedule_times"]) == {"quant_signal", "quant_execute"}
    finally:
        db.close()


def test_config_update_rejects_live_and_disables_plans(tmp_path: Path):
    db = setup_db(tmp_path)
    try:
        with pytest.raises(HTTPException, match="仅支持模拟盘"):
            update_quant_strategy(
                "multi_factor_core",
                QuantStrategyConfigUpdate(mode="LIVE", parameters={}),
                None,
                db,
            )

        config = db.scalar(select(StrategyConfig).where(StrategyConfig.name == "多因子核心组合"))
        schedules = list(
            db.scalars(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id == config.id
                )
            )
        )
        for schedule in schedules:
            schedule.enabled = True
        db.commit()
        result = update_quant_strategy(
            "multi_factor_core",
            QuantStrategyConfigUpdate(
                mode="SIMULATION",
                parameters={**config.parameters, "prefilter_size": 500},
            ),
            None,
            db,
        )

        assert result["parameters"]["prefilter_size"] == 500
        assert all(not item.enabled for item in schedules)
    finally:
        db.close()


def test_backtest_api_enqueues_real_data_task(tmp_path: Path):
    db = setup_db(tmp_path)
    try:
        result = create_quant_backtest(
            "multi_factor_core",
            QuantBacktestCreate(start_date="2023-01-01", end_date="2026-07-23"),
            None,
            db,
        )
        task = db.get(QuantStrategyTask, result["task_id"])

        assert result["status"] == "pending"
        assert task.task_type == "backtest"
        assert task.payload["start_date"] == "2023-01-01"
        assert task.payload["end_date"] == "2026-07-23"
    finally:
        db.close()


def test_backtest_api_validates_dates_and_keeps_ranges_independent(tmp_path: Path):
    db = setup_db(tmp_path)
    try:
        first = create_quant_backtest(
            "multi_factor_core",
            QuantBacktestCreate(start_date="2023-01-01", end_date="2026-07-23"),
            None,
            db,
        )
        second = create_quant_backtest(
            "multi_factor_core",
            QuantBacktestCreate(start_date="2022-01-01", end_date="2026-07-23"),
            None,
            db,
        )

        assert first["task_id"] != second["task_id"]
        with pytest.raises(HTTPException, match="日期格式"):
            create_quant_backtest(
                "multi_factor_core",
                QuantBacktestCreate(start_date="not-a-date", end_date="2026-07-23"),
                None,
                db,
            )
    finally:
        db.close()


def test_backtest_task_idempotency_includes_current_configuration(tmp_path: Path):
    db = setup_db(tmp_path)
    try:
        payload = QuantBacktestCreate(
            start_date="2023-01-01",
            end_date="2026-07-23",
        )
        first = create_quant_backtest(
            "multi_factor_core",
            payload,
            None,
            db,
        )
        duplicate = create_quant_backtest(
            "multi_factor_core",
            payload,
            None,
            db,
        )
        config = db.scalar(
            select(StrategyConfig).where(
                StrategyConfig.name == "多因子核心组合"
            )
        )
        config.parameters = {**config.parameters, "prefilter_size": 500}
        db.commit()
        changed = create_quant_backtest(
            "multi_factor_core",
            payload,
            None,
            db,
        )

        assert duplicate["task_id"] == first["task_id"]
        assert changed["task_id"] != first["task_id"]
    finally:
        db.close()


def test_dry_run_uses_separate_decision_and_never_creates_orders(
    tmp_path: Path,
    monkeypatch,
):
    db = setup_db(tmp_path)
    try:
        decision = SimpleNamespace(
            id=81,
            status="ready",
            target_weights={"000001.SZ": 0.1},
            snapshot_sha256="a" * 64,
            config_fingerprint="b" * 64,
            strategy_version="1.0.0",
            data_version="1",
        )
        run = SimpleNamespace(
            id=91,
            summary={"dry_run": True, "order_ids": [], "accepted": 1},
        )
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        db.add(
            MarketDailyBar(
                stock_id=stock.id,
                trade_date=CURRENT.date().isoformat(),
                open=10,
                high=11,
                low=9,
                close=10,
                adjusted_close=10,
                adjustment_factor=1,
                volume=1,
                amount=1,
                source="test-real",
            )
        )
        db.commit()
        monkeypatch.setattr("app.main.now", lambda: CURRENT)
        monkeypatch.setattr("app.main.build_signal_decision", lambda *args, **kwargs: decision)
        monkeypatch.setattr("app.main.execute_quant_rebalance", lambda *args, **kwargs: run)
        monkeypatch.setattr("app.main.record_dry_run_approval", lambda *args, **kwargs: None)

        result = run_quant_strategy_dry_run("multi_factor_core", None, db)

        assert result["order_ids"] == []
        assert result["decision_id"] == 81
        assert result["strategy_run_id"] == 91
    finally:
        db.close()


def test_dry_run_uses_latest_completed_market_date_for_signal_data(
    tmp_path: Path,
    monkeypatch,
):
    db = setup_db(tmp_path)
    captured = {}
    try:
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        db.add(
            MarketDailyBar(
                stock_id=stock.id,
                trade_date="2026-07-24",
                open=10,
                high=11,
                low=9,
                close=10,
                adjusted_close=10,
                adjustment_factor=1,
                volume=1,
                amount=1,
                source="test-real",
            )
        )
        db.commit()
        decision = SimpleNamespace(
            id=84,
            status="ready",
            target_weights={},
            snapshot_sha256="a" * 64,
            config_fingerprint="b" * 64,
            strategy_version="1.0.0",
            data_version="1",
        )
        run = SimpleNamespace(
            id=94,
            summary={
                "dry_run": True,
                "order_ids": [],
                "accepted": 0,
                "precheck_passed": True,
            },
        )
        monday = datetime(2026, 7, 27, 9, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
        monkeypatch.setattr("app.main.now", lambda: monday)

        def build(*_args, **kwargs):
            captured.update(kwargs)
            return decision

        monkeypatch.setattr("app.main.build_signal_decision", build)
        monkeypatch.setattr("app.main.execute_quant_rebalance", lambda *args, **kwargs: run)
        monkeypatch.setattr("app.main.record_dry_run_approval", lambda *args, **kwargs: None)

        run_quant_strategy_dry_run("multi_factor_core", None, db)

        assert captured["current"] == monday
        assert captured["as_of"] == datetime(2026, 7, 24).date()
        assert captured["decision_type"] == "dry_run"
    finally:
        db.close()


def test_blocked_dry_run_is_not_approved(tmp_path: Path, monkeypatch):
    db = setup_db(tmp_path)
    approvals = []
    try:
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        db.add(
            MarketDailyBar(
                stock_id=stock.id,
                trade_date=CURRENT.date().isoformat(),
                open=10,
                high=11,
                low=9,
                close=10,
                adjusted_close=10,
                adjustment_factor=1,
                volume=1,
                amount=1,
                source="test-real",
            )
        )
        db.commit()
        decision = SimpleNamespace(
            id=82,
            status="blocked",
            target_weights={"000001.SZ": 0.1},
            snapshot_sha256="a" * 64,
            config_fingerprint="b" * 64,
            strategy_version="1.0.0",
            data_version="1",
        )
        run = SimpleNamespace(
            id=92,
            summary={
                "dry_run": True,
                "order_ids": [],
                "accepted": 0,
                "reason": "000001.SZ 行情已过期",
            },
        )
        monkeypatch.setattr("app.main.now", lambda: CURRENT)
        monkeypatch.setattr("app.main.build_signal_decision", lambda *args, **kwargs: decision)
        monkeypatch.setattr("app.main.execute_quant_rebalance", lambda *args, **kwargs: run)
        monkeypatch.setattr(
            "app.main.record_dry_run_approval",
            lambda *args, **kwargs: approvals.append(True),
        )

        with pytest.raises(HTTPException, match="行情已过期"):
            run_quant_strategy_dry_run("multi_factor_core", None, db)

        assert approvals == []
    finally:
        db.close()


def test_valid_zero_order_dry_run_can_be_approved(tmp_path: Path, monkeypatch):
    db = setup_db(tmp_path)
    approvals = []
    try:
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        db.add(
            MarketDailyBar(
                stock_id=stock.id,
                trade_date=CURRENT.date().isoformat(),
                open=10,
                high=11,
                low=9,
                close=10,
                adjusted_close=10,
                adjustment_factor=1,
                volume=1,
                amount=1,
                source="test-real",
            )
        )
        db.commit()
        decision = SimpleNamespace(
            id=83,
            status="ready",
            target_weights={},
            snapshot_sha256="a" * 64,
            config_fingerprint="b" * 64,
            strategy_version="1.0.0",
            data_version="1",
        )
        run = SimpleNamespace(
            id=93,
            summary={
                "dry_run": True,
                "order_ids": [],
                "accepted": 0,
                "precheck_passed": True,
            },
        )
        monkeypatch.setattr("app.main.now", lambda: CURRENT)
        monkeypatch.setattr("app.main.build_signal_decision", lambda *args, **kwargs: decision)
        monkeypatch.setattr("app.main.execute_quant_rebalance", lambda *args, **kwargs: run)
        monkeypatch.setattr(
            "app.main.record_dry_run_approval",
            lambda *args, **kwargs: approvals.append(True),
        )

        result = run_quant_strategy_dry_run("multi_factor_core", None, db)

        assert result["order_ids"] == []
        assert approvals == [True]
    finally:
        db.close()


def test_activation_fails_closed_until_readiness_passes(tmp_path: Path):
    db = setup_db(tmp_path)
    try:
        readiness = get_quant_strategy_readiness("multi_factor_core", None, db)
        with pytest.raises(HTTPException, match="上线闸门"):
            activate_quant_strategy("multi_factor_core", None, db)

        assert readiness["automation_ready"] is False
        config = db.scalar(select(StrategyConfig).where(StrategyConfig.name == "多因子核心组合"))
        schedules = list(
            db.scalars(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id == config.id
                )
            )
        )
        assert all(not item.enabled for item in schedules)
    finally:
        db.close()


def test_pause_disables_only_selected_strategy(tmp_path: Path):
    db = setup_db(tmp_path)
    try:
        configs = list(db.scalars(select(StrategyConfig)))
        target = next(item for item in configs if item.name == "多因子核心组合")
        other = next(item for item in configs if item.name == "相对强弱轮动")
        for schedule in db.scalars(select(StrategySchedule)):
            if schedule.strategy_config_id in {target.id, other.id}:
                schedule.enabled = True
        db.add(
            NotificationChannel(
                type="email",
                name="策略运营",
                enabled=True,
                recipient="ops@example.com",
                secret_ref="SMTP_PASSWORD",
                event_types=["quant_strategy_paused"],
            )
        )
        db.commit()

        result = pause_quant_strategy("multi_factor_core", None, db)

        target_schedules = list(db.scalars(select(StrategySchedule).where(StrategySchedule.strategy_config_id == target.id)))
        other_schedules = list(db.scalars(select(StrategySchedule).where(StrategySchedule.strategy_config_id == other.id)))
        assert result["status"] == "PAUSED"
        assert all(not item.enabled for item in target_schedules)
        assert all(item.enabled for item in other_schedules)
        delivery = db.scalar(
            select(NotificationDelivery).where(
                NotificationDelivery.event_type == "quant_strategy_paused"
            )
        )
        assert delivery is not None
        assert delivery.payload["strategy_key"] == "multi_factor_core"
    finally:
        db.close()


def test_generic_schedule_endpoint_cannot_bypass_quant_readiness(tmp_path: Path):
    db = setup_db(tmp_path)
    try:
        config = db.scalar(
            select(StrategyConfig).where(StrategyConfig.name == "多因子核心组合")
        )
        schedule = db.scalar(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id,
                StrategySchedule.trigger_type == "quant_signal",
            )
        )

        with pytest.raises(HTTPException, match="独立量化策略专用接口"):
            update_schedule(
                schedule.id,
                ScheduleUpdate(enabled=True),
                None,
                db,
            )

        db.refresh(schedule)
        assert schedule.enabled is False

        with pytest.raises(HTTPException, match="独立量化策略专用接口"):
            delete_schedule(schedule.id, None, db)

        assert db.get(StrategySchedule, schedule.id) is not None
    finally:
        db.close()


def test_paused_strategy_can_resume_only_after_other_gates_pass(
    tmp_path: Path,
    monkeypatch,
):
    db = setup_db(tmp_path)
    try:
        config = db.scalar(
            select(StrategyConfig).where(StrategyConfig.name == "多因子核心组合")
        )
        risk = db.scalar(
            select(StrategyRiskProfile).where(
                StrategyRiskProfile.strategy_config_id == config.id
            )
        )
        risk.emergency_stop_enabled = True
        db.commit()

        def readiness(session, config_id):
            current_risk = session.scalar(
                select(StrategyRiskProfile).where(
                    StrategyRiskProfile.strategy_config_id == config_id
                )
            )
            paused = current_risk.emergency_stop_enabled
            return {
                "status": "PAUSED" if paused else "READY",
                "ready": True,
                "automation_ready": not paused,
                "reasons": [],
            }

        monkeypatch.setattr("app.main.quant_strategy_readiness", readiness)

        result = activate_quant_strategy("multi_factor_core", None, db)

        assert result["automation_ready"] is True
        assert risk.emergency_stop_enabled is False
        schedules = list(
            db.scalars(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id == config.id
                )
            )
        )
        assert all(item.enabled for item in schedules)
    finally:
        db.close()


def test_failed_strategy_can_be_acknowledged_and_resumed_by_admin(
    tmp_path: Path,
    monkeypatch,
):
    db = setup_db(tmp_path)
    try:
        config = db.scalar(
            select(StrategyConfig).where(
                StrategyConfig.name == "多因子核心组合"
            )
        )
        risk = db.scalar(
            select(StrategyRiskProfile).where(
                StrategyRiskProfile.strategy_config_id == config.id
            )
        )
        risk.consecutive_errors = risk.max_consecutive_errors
        for schedule in db.scalars(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id
            )
        ):
            schedule.enabled = False
        db.commit()

        def readiness(session, config_id):
            current_risk = session.scalar(
                select(StrategyRiskProfile).where(
                    StrategyRiskProfile.strategy_config_id == config_id
                )
            )
            failed = current_risk.consecutive_errors >= current_risk.max_consecutive_errors
            return {
                "status": "FAILED" if failed else "READY",
                "ready": True,
                "automation_ready": not failed,
                "reasons": [],
            }

        monkeypatch.setattr("app.main.quant_strategy_readiness", readiness)

        result = activate_quant_strategy("multi_factor_core", None, db)

        assert result["automation_ready"] is True
        assert risk.consecutive_errors == 0
        schedules = list(
            db.scalars(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id == config.id
                )
            )
        )
        assert all(schedule.enabled for schedule in schedules)
    finally:
        db.close()


def test_activation_queues_strategy_enabled_notification(
    tmp_path: Path,
    monkeypatch,
):
    db = setup_db(tmp_path)
    try:
        db.add(
            NotificationChannel(
                type="email",
                name="策略运营",
                enabled=True,
                recipient="ops@example.com",
                secret_ref="SMTP_PASSWORD",
                event_types=["quant_strategy_activated"],
            )
        )
        db.commit()
        monkeypatch.setattr(
            "app.main.quant_strategy_readiness",
            lambda *_args, **_kwargs: {
                "status": "READY",
                "ready": True,
                "automation_ready": True,
                "reasons": [],
            },
        )

        result = activate_quant_strategy("multi_factor_core", None, db)

        assert result["automation_ready"] is True
        delivery = db.scalar(
            select(NotificationDelivery).where(
                NotificationDelivery.event_type == "quant_strategy_activated"
            )
        )
        assert delivery is not None
        assert delivery.payload == {
            "strategy_key": "multi_factor_core",
            "strategy_config_id": delivery.payload["strategy_config_id"],
        }
    finally:
        db.close()


def test_detail_includes_latest_candidate_position_order_and_qualification(
    tmp_path: Path,
):
    db = setup_db(tmp_path)
    try:
        config = db.scalar(
            select(StrategyConfig).where(StrategyConfig.name == "多因子核心组合")
        )
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        run = StrategyRun(
            strategy_config_id=config.id,
            mode="SIMULATION",
            status="completed",
            started_at=CURRENT,
            finished_at=CURRENT,
            summary={"accepted": 0},
        )
        db.add(run)
        db.flush()
        decision = QuantPortfolioDecision(
            strategy_run_id=run.id,
            strategy_config_id=config.id,
            simulation_account_id=config.simulation_account_id,
            trading_date="2026-07-24",
            decision_type="signal",
            status="ready",
            data_as_of=CURRENT,
            config_fingerprint="a" * 64,
            strategy_version="1.0.0",
            data_version="1",
            target_weights={stock.symbol: 0.1},
            snapshot_payload={"as_of": "2026-07-24", "sources": ["test-real"]},
        )
        db.add(decision)
        db.flush()
        db.add(
            QuantCandidateScore(
                decision_id=decision.id,
                stock_id=stock.id,
                status="selected",
                rank=1,
                score=0.8,
                target_weight=0.1,
            )
        )
        db.add(
            Position(
                account_id=config.simulation_account_id,
                mode="SIMULATION",
                stock_id=stock.id,
                quantity=100,
                available_quantity=100,
                average_cost=10,
                market_value=1000,
            )
        )
        db.add(
            Order(
                account_id=config.simulation_account_id,
                mode="SIMULATION",
                stock_id=stock.id,
                side="buy",
                quantity=100,
                status="filled",
            )
        )
        db.add(
            RiskEvent(
                mode="SIMULATION",
                event_type="quant_strategy_rebalance_blocked",
                strategy_run_id=run.id,
                message="测试风控事件",
                context={"strategy_config_id": config.id},
            )
        )
        db.add(
            StrategyBacktestQualification(
                strategy_config_id=config.id,
                config_fingerprint="a" * 64,
                strategy_version="1.0.0",
                data_version="1",
                trading_days=500,
                data_completeness=0.99,
                out_of_sample_annualized_return=0.1,
                sharpe_ratio=0.5,
                max_drawdown=-0.1,
                trade_count=40,
                qualified=True,
            )
        )
        db.commit()

        detail = get_quant_strategy("multi_factor_core", None, db)

        assert detail["latest_candidates"][0]["symbol"] == stock.symbol
        assert detail["positions"][0]["symbol"] == stock.symbol
        assert detail["orders"][0]["symbol"] == stock.symbol
        assert detail["qualifications"][0]["qualified"] is True
        assert detail["decisions"][0]["snapshot_payload"]["as_of"] == "2026-07-24"
        assert detail["risk_events"][0]["message"] == "测试风控事件"
    finally:
        db.close()
