from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.main import (
    ProbabilityPortfolioConfigUpdate,
    ScheduleUpdate,
    get_probability_portfolio_readiness,
    get_probability_portfolio_run,
    list_probability_portfolio_runs,
    run_probability_portfolio_dry_run,
    update_schedule,
    update_probability_portfolio_config,
)
from app.models import (
    Order,
    ProbabilityModelArtifact,
    Stock,
    StrategySchedule,
)
from app.probability_portfolio.candidates import CandidateBuildResult
from app.probability_portfolio.execution import ScoredCandidate
from app.probability_portfolio.features import FEATURE_NAMES
from app.probability_portfolio.runtime import seed_probability_portfolio_runtime
from app.services import seed_database


CURRENT = datetime(2026, 7, 23, 14, 40, tzinfo=ZoneInfo("Asia/Shanghai"))


def setup_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}")
    Base.metadata.create_all(engine)
    db = Session(engine)
    settings = Settings(database_url=str(engine.url))
    seed_database(db, settings)
    config = seed_probability_portfolio_runtime(db, settings)
    return db, config


def add_ready_artifact(db):
    artifact = ProbabilityModelArtifact(
        model_version="api-ready-model",
        feature_version="1",
        status="ready",
        trained_through="2026-07-22",
        training_sample_count=500,
        calibration_sample_count=100,
        calibration_start="2026-04-01",
        calibration_end="2026-07-22",
        brier_score=0.20,
        coefficients={
            "feature_names": list(FEATURE_NAMES),
            "model": {
                "intercept": 0.5,
                "weights": [0.0] * len(FEATURE_NAMES),
                "means": [0.0] * len(FEATURE_NAMES),
                "scales": [1.0] * len(FEATURE_NAMES),
            },
            "average_win": 0.02,
            "average_loss": -0.01,
        },
        calibration_curve=[
            {"raw": 0.0, "calibrated": 0.60},
            {"raw": 1.0, "calibrated": 0.70},
        ],
        artifact_sha256="c" * 64,
    )
    db.add(artifact)
    db.commit()
    return artifact


def test_readiness_reports_model_data_account_and_schedule_state(tmp_path):
    db, config = setup_db(tmp_path)
    try:
        result = get_probability_portfolio_readiness(None, db)

        assert result["ready"] is False
        assert result["simulation_only"] is True
        assert result["account_id"] == config.simulation_account_id
        assert result["initial_cash"] == 2_000_000
        assert result["model_ready"] is False
        assert "model" in result["reasons"]
        assert result["entry_schedule_enabled"] is False
        assert result["exit_schedule_enabled"] is False
    finally:
        db.close()


def test_readiness_rejects_model_with_mismatched_feature_contract(tmp_path):
    db, _ = setup_db(tmp_path)
    try:
        artifact = add_ready_artifact(db)
        artifact.coefficients = {
            **artifact.coefficients,
            "feature_names": list(FEATURE_NAMES[:-1]),
        }
        db.commit()

        result = get_probability_portfolio_readiness(None, db)

        assert result["model_ready"] is False
        assert "model" in result["reasons"]
    finally:
        db.close()


def test_readiness_rejects_invalid_calibration_curve_contract(tmp_path):
    db, _ = setup_db(tmp_path)
    try:
        artifact = add_ready_artifact(db)
        artifact.calibration_curve = [
            {"raw": 0.2, "calibrated": 0.8},
            {"raw": 0.8, "calibrated": 0.4},
        ]
        db.commit()

        result = get_probability_portfolio_readiness(None, db)

        assert result["model_ready"] is False
        assert "model" in result["reasons"]
    finally:
        db.close()


def test_readiness_accepts_monotonic_calibration_curve_with_tied_raw_points(
    tmp_path,
):
    db, _ = setup_db(tmp_path)
    try:
        artifact = add_ready_artifact(db)
        artifact.calibration_curve = [
            {"raw": 0.2, "calibrated": 0.55},
            {"raw": 0.2, "calibrated": 0.55},
            {"raw": 0.8, "calibrated": 0.70},
        ]
        db.commit()

        result = get_probability_portfolio_readiness(None, db)

        assert result["model_ready"] is True
    finally:
        db.close()


def test_readiness_does_not_fall_back_when_newest_model_is_rejected(tmp_path):
    db, _ = setup_db(tmp_path)
    try:
        ready = add_ready_artifact(db)
        rejected = ProbabilityModelArtifact(
            model_version="api-newer-rejected-model",
            feature_version="1",
            status="rejected",
            trained_through="2026-07-23",
            training_sample_count=600,
            calibration_sample_count=120,
            calibration_start="2026-05-01",
            calibration_end="2026-07-23",
            brier_score=0.30,
            coefficients=ready.coefficients,
            calibration_curve=ready.calibration_curve,
            artifact_sha256="d" * 64,
            error_message="brier_score",
        )
        db.add(rejected)
        db.commit()

        result = get_probability_portfolio_readiness(None, db)

        assert result["model_ready"] is False
        assert result["model_version"] is None
    finally:
        db.close()


def test_readiness_keeps_hard_model_thresholds_when_database_config_is_unsafe(
    tmp_path,
):
    db, config = setup_db(tmp_path)
    try:
        artifact = add_ready_artifact(db)
        artifact.training_sample_count = 8
        artifact.calibration_sample_count = 2
        artifact.brier_score = 0.90
        config.parameters = {
            **config.parameters,
            "min_training_samples": 1,
            "min_calibration_samples": 1,
            "max_brier_score": 1.0,
        }
        db.commit()

        result = get_probability_portfolio_readiness(None, db)

        assert result["model_ready"] is False
        assert "model" in result["reasons"]
    finally:
        db.close()


def test_config_api_rejects_live_and_keeps_entry_disabled_without_model(tmp_path):
    db, _ = setup_db(tmp_path)
    try:
        with pytest.raises(HTTPException, match="仅支持模拟盘"):
            update_probability_portfolio_config(
                ProbabilityPortfolioConfigUpdate(mode="LIVE", parameters={}),
                None,
                db,
            )

        result = update_probability_portfolio_config(
            ProbabilityPortfolioConfigUpdate(
                mode="SIMULATION",
                parameters={
                    "max_positions": 8,
                    "min_position_pct": 0.03,
                    "max_position_pct": 0.30,
                    "max_total_exposure_pct": 0.55,
                    "exit_time": "10:30",
                    "latest_exit_time": "10:45",
                    "dry_run": False,
                },
            ),
            None,
            db,
        )
        entry = db.scalar(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == result["id"],
                StrategySchedule.trigger_type == "portfolio_entry",
            )
        )

        assert result["mode"] == "SIMULATION"
        assert result["parameters"]["max_positions"] == 8
        assert entry.enabled is False
    finally:
        db.close()


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ({"min_probability": 0.54}, "盈利概率"),
        ({"min_expected_net_return": -0.01}, "预期净收益"),
        ({"min_position_pct": 0.01}, "单股仓位"),
        ({"max_position_pct": 0.37}, "单股仓位"),
        ({"min_total_exposure_pct": 0.61}, "组合总仓位"),
        ({"max_total_exposure_pct": 0.61}, "组合总仓位"),
        ({"prefilter_size": 101}, "预筛数量"),
        ({"entry_time": "14:39"}, "入场时间"),
        ({"retry_seconds": 30}, "重试间隔"),
        ({"min_training_samples": 499}, "训练样本"),
        ({"min_calibration_samples": 99}, "校准样本"),
        ({"max_brier_score": 0.26}, "Brier"),
        ({"feature_version": "2"}, "特征版本"),
        ({"daily_loss_limit_pct": 0}, "日亏损"),
        ({"daily_loss_limit_pct": 0.11}, "日亏损"),
        ({"quote_max_age_seconds": 61}, "行情新鲜度"),
        ({"event_max_age_seconds": 1801}, "公告新鲜度"),
    ],
)
def test_config_api_rejects_parameters_that_weaken_strategy_gates(
    tmp_path,
    parameters,
    message,
):
    db, _ = setup_db(tmp_path)
    try:
        with pytest.raises(HTTPException, match=message):
            update_probability_portfolio_config(
                ProbabilityPortfolioConfigUpdate(parameters=parameters),
                None,
                db,
            )
    finally:
        db.close()


def test_dry_run_api_writes_audit_but_no_orders(tmp_path, monkeypatch):
    db, config = setup_db(tmp_path)
    try:
        monkeypatch.setattr("app.main.now", lambda: CURRENT)
        result = run_probability_portfolio_dry_run(None, db)
        count = db.scalar(select(Order.id).where(Order.account_id == config.simulation_account_id))

        assert result["summary"]["dry_run"] is True
        assert result["summary"]["order_ids"] == []
        assert count is None
        runs = list_probability_portfolio_runs(30, None, db)
        assert len(runs) == 1
        assert runs[0]["trigger_type"].startswith("portfolio_dry_")
    finally:
        db.close()


def test_successful_dry_run_unlocks_entry_until_parameters_change(
    tmp_path,
    monkeypatch,
):
    db, config = setup_db(tmp_path)
    try:
        artifact = add_ready_artifact(db)
        stock = db.scalar(select(Stock).where(Stock.exchange == "SSE"))
        scored = ScoredCandidate(
            stock_id=stock.id,
            symbol=stock.symbol,
            features={name: 0.01 for name in FEATURE_NAMES},
            raw_probability=0.66,
            calibrated_probability=0.64,
            expected_net_return=0.012,
            volatility_20d=0.08,
        )
        monkeypatch.setattr("app.main.now", lambda: CURRENT)
        monkeypatch.setattr(
            "app.main.build_scored_candidates",
            lambda *args, **kwargs: CandidateBuildResult(
                [scored], [], (), artifact.id
            ),
        )

        dry_run = run_probability_portfolio_dry_run(None, db)
        detail = get_probability_portfolio_run(
            dry_run["summary"]["portfolio_run_id"], None, db
        )
        assert detail["decisions"][0]["calibrated_probability"] == 0.64
        assert detail["orders"] == []

        update_probability_portfolio_config(
            ProbabilityPortfolioConfigUpdate(
                parameters={**config.parameters, "dry_run": False}
            ),
            None,
            db,
        )
        readiness = get_probability_portfolio_readiness(None, db)
        entry = db.scalar(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id,
                StrategySchedule.trigger_type == "portfolio_entry",
            )
        )

        assert readiness["model_ready"] is True
        assert readiness["dry_run_validated"] is True
        assert readiness["automation_ready"] is True
        exit_schedule = db.scalar(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id,
                StrategySchedule.trigger_type == "portfolio_exit",
            )
        )
        update_schedule(exit_schedule.id, ScheduleUpdate(enabled=True), None, db)
        update_schedule(entry.id, ScheduleUpdate(enabled=True), None, db)
        assert entry.enabled is True

        update_probability_portfolio_config(
            ProbabilityPortfolioConfigUpdate(
                parameters={**config.parameters, "max_positions": 8}
            ),
            None,
            db,
        )
        readiness = get_probability_portfolio_readiness(None, db)

        assert entry.enabled is False
        assert readiness["dry_run_validated"] is False
        assert readiness["automation_ready"] is False
    finally:
        db.close()


def test_automatic_training_observation_cannot_unlock_entry_schedule(
    tmp_path,
    monkeypatch,
):
    db, config = setup_db(tmp_path)
    try:
        artifact = add_ready_artifact(db)
        stock = db.scalar(select(Stock).where(Stock.exchange == "SSE"))
        scored = ScoredCandidate(
            stock_id=stock.id,
            symbol=stock.symbol,
            features={name: 0.01 for name in FEATURE_NAMES},
            raw_probability=0.66,
            calibrated_probability=0.64,
            expected_net_return=0.012,
            volatility_20d=0.08,
        )
        from app.probability_portfolio.execution import execute_portfolio_entry

        execute_portfolio_entry(
            db,
            config,
            current=CURRENT,
            scored_candidates=[scored],
            trigger_type="portfolio_observation",
            dry_run=True,
        )
        config.parameters = {**config.parameters, "dry_run": False}
        db.commit()
        monkeypatch.setattr("app.main.now", lambda: CURRENT)

        readiness = get_probability_portfolio_readiness(None, db)

        assert artifact.id is not None
        assert readiness["model_ready"] is True
        assert readiness["dry_run_validated"] is False
        assert readiness["automation_ready"] is False
    finally:
        db.close()


def test_dry_run_must_use_current_model_and_1440_decision_window(
    tmp_path,
    monkeypatch,
):
    db, config = setup_db(tmp_path)
    try:
        first_artifact = add_ready_artifact(db)
        stock = db.scalar(select(Stock).where(Stock.exchange == "SSE"))
        scored = ScoredCandidate(
            stock_id=stock.id,
            symbol=stock.symbol,
            features={name: 0.01 for name in FEATURE_NAMES},
            raw_probability=0.66,
            calibrated_probability=0.64,
            expected_net_return=0.012,
            volatility_20d=0.08,
        )
        from app.probability_portfolio.execution import execute_portfolio_entry

        off_window = CURRENT.replace(hour=13, minute=0)
        execute_portfolio_entry(
            db,
            config,
            current=off_window,
            scored_candidates=[scored],
            trigger_type="portfolio_dry_off_window",
            dry_run=True,
        )
        config.parameters = {**config.parameters, "dry_run": False}
        db.commit()
        monkeypatch.setattr("app.main.now", lambda: CURRENT)

        assert get_probability_portfolio_readiness(None, db)["dry_run_validated"] is False

        execute_portfolio_entry(
            db,
            config,
            current=CURRENT,
            scored_candidates=[scored],
            trigger_type="portfolio_dry_valid",
            dry_run=True,
        )
        assert get_probability_portfolio_readiness(None, db)["dry_run_validated"] is True

        second_artifact = ProbabilityModelArtifact(
            model_version="api-newer-ready-model",
            feature_version="1",
            status="ready",
            trained_through="2026-07-23",
            training_sample_count=600,
            calibration_sample_count=120,
            calibration_start="2026-05-01",
            calibration_end="2026-07-23",
            brier_score=0.20,
            coefficients=first_artifact.coefficients,
            calibration_curve=first_artifact.calibration_curve,
            artifact_sha256="e" * 64,
        )
        db.add(second_artifact)
        db.commit()

        readiness = get_probability_portfolio_readiness(None, db)

        assert readiness["model_version"] == second_artifact.model_version
        assert readiness["dry_run_validated"] is False
        assert readiness["automation_ready"] is False
    finally:
        db.close()


def test_entry_schedule_cannot_be_enabled_before_readiness(tmp_path):
    db, config = setup_db(tmp_path)
    try:
        entry = db.scalar(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id,
                StrategySchedule.trigger_type == "portfolio_entry",
            )
        )

        with pytest.raises(HTTPException, match="自动计划不能启用"):
            update_schedule(entry.id, ScheduleUpdate(enabled=True), None, db)
    finally:
        db.close()


def test_entry_schedule_requires_exit_schedule_and_exit_disable_stops_entry(
    tmp_path,
    monkeypatch,
):
    db, config = setup_db(tmp_path)
    try:
        entry = db.scalar(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id,
                StrategySchedule.trigger_type == "portfolio_entry",
            )
        )
        exit_schedule = db.scalar(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id,
                StrategySchedule.trigger_type == "portfolio_exit",
            )
        )
        monkeypatch.setattr(
            "app.main.probability_automation_readiness",
            lambda *args, **kwargs: {"automation_ready": True, "automation_reasons": []},
        )

        with pytest.raises(HTTPException, match="退出计划"):
            update_schedule(entry.id, ScheduleUpdate(enabled=True), None, db)

        update_schedule(exit_schedule.id, ScheduleUpdate(enabled=True), None, db)
        update_schedule(entry.id, ScheduleUpdate(enabled=True), None, db)
        assert entry.enabled is True

        update_schedule(exit_schedule.id, ScheduleUpdate(enabled=False), None, db)
        db.refresh(entry)
        assert entry.enabled is False
    finally:
        db.close()
