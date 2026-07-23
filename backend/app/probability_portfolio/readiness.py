from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (
    ProbabilityModelArtifact,
    ProbabilityPortfolioRun,
    SimulationAccount,
    StrategyConfig,
    StrategyRun,
)
from .config import PROBABILITY_PORTFOLIO_DEFAULTS
from .features import FEATURE_NAMES


def _valid_artifact_contract(artifact: ProbabilityModelArtifact) -> bool:
    value = artifact.coefficients or {}
    model = value.get("model")
    if tuple(value.get("feature_names") or ()) != FEATURE_NAMES or not isinstance(model, dict):
        return False
    expected_size = len(FEATURE_NAMES)
    try:
        intercept = float(model["intercept"])
        weights = [float(item) for item in model["weights"]]
        means = [float(item) for item in model["means"]]
        scales = [float(item) for item in model["scales"]]
        average_win = float(value["average_win"])
        average_loss = float(value["average_loss"])
    except (KeyError, TypeError, ValueError):
        return False
    values = [intercept, average_win, average_loss, *weights, *means, *scales]
    curve = artifact.calibration_curve or []
    try:
        raw_points = [float(point["raw"]) for point in curve]
        calibrated_points = [float(point["calibrated"]) for point in curve]
    except (KeyError, TypeError, ValueError):
        return False
    valid_curve = bool(curve) and all(
        math.isfinite(value) and 0 <= value <= 1
        for value in [*raw_points, *calibrated_points]
    )
    return (
        len(weights) == len(means) == len(scales) == expected_size
        and all(math.isfinite(item) for item in values)
        and all(item > 0 for item in scales)
        and valid_curve
        and raw_points == sorted(raw_points)
        and calibrated_points == sorted(calibrated_points)
    )


def configuration_fingerprint(
    parameters: dict[str, Any] | None,
    *,
    simulation_account_id: int | None,
) -> str:
    normalized = {**PROBABILITY_PORTFOLIO_DEFAULTS, **(parameters or {})}
    normalized.pop("dry_run", None)
    payload = json.dumps(
        {
            "parameters": normalized,
            "simulation_account_id": simulation_account_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def model_artifact_reasons(
    artifact: ProbabilityModelArtifact | None,
    parameters: dict[str, Any],
    *,
    current: datetime,
) -> list[str]:
    if artifact is None:
        return ["model_missing"]
    reasons: list[str] = []
    if artifact.status != "ready":
        reasons.append("model_status")
    if artifact.feature_version != str(parameters["feature_version"]):
        reasons.append("feature_version")
    min_training_samples = max(
        int(PROBABILITY_PORTFOLIO_DEFAULTS["min_training_samples"]),
        int(parameters["min_training_samples"]),
    )
    min_calibration_samples = max(
        int(PROBABILITY_PORTFOLIO_DEFAULTS["min_calibration_samples"]),
        int(parameters["min_calibration_samples"]),
    )
    max_brier_score = min(
        float(PROBABILITY_PORTFOLIO_DEFAULTS["max_brier_score"]),
        float(parameters["max_brier_score"]),
    )
    if artifact.training_sample_count < min_training_samples:
        reasons.append("training_samples")
    if artifact.calibration_sample_count < min_calibration_samples:
        reasons.append("calibration_samples")
    if (
        artifact.brier_score is None
        or artifact.brier_score > max_brier_score
    ):
        reasons.append("brier_score")
    try:
        trained_through = date.fromisoformat(artifact.trained_through)
    except (TypeError, ValueError):
        reasons.append("trained_through")
    else:
        if trained_through > current.date():
            reasons.append("future_training_data")
    if not artifact.artifact_sha256 or not _valid_artifact_contract(artifact):
        reasons.append("artifact_contract")
    return list(dict.fromkeys(reasons))


def latest_qualified_artifact(
    db: Session,
    parameters: dict[str, Any] | None,
    *,
    current: datetime,
) -> ProbabilityModelArtifact | None:
    normalized = {**PROBABILITY_PORTFOLIO_DEFAULTS, **(parameters or {})}
    artifact = db.scalar(
        select(ProbabilityModelArtifact)
        .where(
            ProbabilityModelArtifact.feature_version
            == str(normalized["feature_version"])
        )
        .order_by(
            ProbabilityModelArtifact.trained_through.desc(),
            ProbabilityModelArtifact.id.desc(),
        )
        .limit(1)
    )
    if artifact is None or model_artifact_reasons(
        artifact,
        normalized,
        current=current,
    ):
        return None
    return artifact


def find_matching_dry_run(
    db: Session,
    config: StrategyConfig,
    *,
    model_artifact_id: int,
) -> ProbabilityPortfolioRun | None:
    fingerprint = configuration_fingerprint(
        config.parameters,
        simulation_account_id=config.simulation_account_id,
    )
    runs = db.scalars(
        select(ProbabilityPortfolioRun)
        .where(
            ProbabilityPortfolioRun.strategy_config_id == config.id,
            ProbabilityPortfolioRun.dry_run.is_(True),
            ProbabilityPortfolioRun.trigger_type.like("portfolio_dry_%"),
            ProbabilityPortfolioRun.status == "completed",
            ProbabilityPortfolioRun.config_fingerprint == fingerprint,
            ProbabilityPortfolioRun.model_artifact_id == model_artifact_id,
        )
        .order_by(ProbabilityPortfolioRun.id.desc())
    )
    for run in runs:
        strategy_run = db.get(StrategyRun, run.strategy_run_id)
        summary = strategy_run.summary if strategy_run else {}
        started_at = strategy_run.started_at if strategy_run else None
        if (
            summary.get("data_ready") is True
            and not run.error_message
            and started_at is not None
            and (started_at.hour, started_at.minute) == (14, 40)
        ):
            return run
    return None


def automation_readiness(
    db: Session,
    config: StrategyConfig,
    settings: Settings,
    *,
    current: datetime,
) -> dict[str, Any]:
    parameters = {**PROBABILITY_PORTFOLIO_DEFAULTS, **(config.parameters or {})}
    account = db.get(SimulationAccount, config.simulation_account_id)
    occupied = db.scalar(
        select(StrategyConfig.id).where(
            StrategyConfig.simulation_account_id == config.simulation_account_id,
            StrategyConfig.id != config.id,
        )
    )
    simulation_only = (
        config.mode == "SIMULATION"
        and not settings.live_enabled
        and settings.broker_adapter == "simulation"
    )
    artifact = latest_qualified_artifact(db, parameters, current=current)
    dry_run = (
        find_matching_dry_run(db, config, model_artifact_id=artifact.id)
        if artifact is not None
        else None
    )
    reasons: list[str] = []
    if not simulation_only:
        reasons.append("simulation_only")
    if account is None or account.status != "active" or occupied is not None:
        reasons.append("simulation_account_binding")
    if not config.enabled:
        reasons.append("strategy_config_disabled")
    if artifact is None:
        reasons.append("model")
    if dry_run is None:
        reasons.append("successful_dry_run")
    if bool(parameters["dry_run"]):
        reasons.append("dry_run_mode_enabled")
    return {
        "automation_ready": not reasons,
        "automation_reasons": reasons,
        "simulation_only": simulation_only,
        "account": account,
        "artifact": artifact,
        "dry_run": dry_run,
        "parameters": parameters,
    }
