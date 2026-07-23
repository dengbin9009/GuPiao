from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ProbabilityModelArtifact, ProbabilityTrainingSample
from .features import FEATURE_NAMES
from .model import ModelSample, train_probability_model


def _completed_cutoff(through: date) -> datetime:
    return datetime.combine(through + timedelta(days=1), time.min)


def train_and_store_probability_model(
    db: Session,
    *,
    through: date,
    feature_version: str,
    min_training_samples: int,
    min_calibration_samples: int,
    max_brier_score: float,
) -> ProbabilityModelArtifact:
    rows = list(
        db.scalars(
            select(ProbabilityTrainingSample)
            .where(
                ProbabilityTrainingSample.feature_version == feature_version,
                ProbabilityTrainingSample.exit_at < _completed_cutoff(through),
            )
            .order_by(
                ProbabilityTrainingSample.entry_at,
                ProbabilityTrainingSample.stock_id,
            )
        )
    )
    samples: list[ModelSample] = []
    invalid_count = 0
    for row in rows:
        try:
            features = {name: float(row.features[name]) for name in FEATURE_NAMES}
            if not all(math.isfinite(value) for value in features.values()):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            invalid_count += 1
            continue
        samples.append(
            ModelSample(
                entry_at=row.entry_at,
                features=features,
                profitable=bool(row.profitable),
                net_return=float(row.net_return),
            )
        )

    trained = train_probability_model(
        samples,
        feature_names=FEATURE_NAMES,
        feature_version=feature_version,
        min_training_samples=min_training_samples,
        min_calibration_samples=min_calibration_samples,
        max_brier_score=max_brier_score,
    )
    model_version = (
        f"probability-{feature_version}-{through.isoformat()}-"
        f"{trained.artifact_sha256[:12]}"
    )
    existing = db.scalar(
        select(ProbabilityModelArtifact).where(
            ProbabilityModelArtifact.model_version == model_version
        )
    )
    if existing is not None:
        return existing

    reasons = list(trained.reasons)
    if invalid_count:
        reasons.append(f"invalid_features:{invalid_count}")
    status = "ready" if not reasons else "rejected"
    artifact = ProbabilityModelArtifact(
        model_version=model_version,
        feature_version=feature_version,
        status=status,
        trained_through=through.isoformat(),
        training_sample_count=trained.training_sample_count,
        calibration_sample_count=trained.calibration_sample_count,
        calibration_start=(
            trained.calibration_start.date().isoformat()
            if trained.calibration_start
            else None
        ),
        calibration_end=(
            trained.calibration_end.date().isoformat()
            if trained.calibration_end
            else None
        ),
        brier_score=trained.brier_score,
        coefficients={
            "feature_names": list(trained.feature_names),
            "model": trained.coefficients,
            "average_win": trained.average_win,
            "average_loss": trained.average_loss,
        },
        calibration_curve=trained.calibration_curve,
        artifact_sha256=trained.artifact_sha256,
        error_message=", ".join(dict.fromkeys(reasons)) or None,
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)
    return artifact
