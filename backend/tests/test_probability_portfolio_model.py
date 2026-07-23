from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.probability_portfolio.model import (
    ModelSample,
    build_window_label,
    model_readiness,
    predict_probability,
    train_probability_model,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def sample(index: int, *, profitable: bool | None = None) -> ModelSample:
    entry = datetime(2024, 1, 2, 14, 40, tzinfo=SHANGHAI) + timedelta(days=index)
    label = bool(index % 3) if profitable is None else profitable
    momentum = (index % 20 - 10) / 100
    return ModelSample(
        entry_at=entry,
        features={
            "momentum_5d": momentum,
            "vwap_distance": momentum / 2,
            "volatility_20d": 0.08 + (index % 7) / 100,
        },
        profitable=label,
        net_return=0.01 if label else -0.008,
    )


def test_window_label_requires_1440_to_next_trading_day_1030():
    entry = datetime(2026, 7, 23, 14, 40, tzinfo=SHANGHAI)
    exit_at = datetime(2026, 7, 24, 10, 30, tzinfo=SHANGHAI)

    result = build_window_label(
        entry_at=entry,
        exit_at=exit_at,
        entry_price=10,
        exit_price=10.2,
        quantity=1000,
        commission_rate=0.0003,
        min_commission=5,
        stamp_tax_rate=0.0005,
        transfer_fee_rate=0,
        slippage_bps=5,
    )

    assert result.profitable is True
    assert result.net_return > 0
    with pytest.raises(ValueError, match="14:40"):
        build_window_label(
            entry_at=entry.replace(hour=15),
            exit_at=exit_at,
            entry_price=10,
            exit_price=10.2,
            quantity=1000,
            commission_rate=0.0003,
            min_commission=5,
            stamp_tax_rate=0.0005,
            transfer_fee_rate=0,
            slippage_bps=5,
        )
    with pytest.raises(ValueError, match="10:30"):
        build_window_label(
            entry_at=entry,
            exit_at=exit_at.replace(hour=9, minute=35),
            entry_price=10,
            exit_price=10.2,
            quantity=1000,
            commission_rate=0.0003,
            min_commission=5,
            stamp_tax_rate=0.0005,
            transfer_fee_rate=0,
            slippage_bps=5,
        )


def test_training_is_time_split_reproducible_and_calibrated_monotonically():
    rows = [sample(index) for index in range(120)]

    first = train_probability_model(
        rows,
        feature_names=("momentum_5d", "vwap_distance", "volatility_20d"),
        feature_version="1",
        min_training_samples=80,
        min_calibration_samples=20,
        max_brier_score=1.0,
    )
    second = train_probability_model(
        list(reversed(rows)),
        feature_names=("momentum_5d", "vwap_distance", "volatility_20d"),
        feature_version="1",
        min_training_samples=80,
        min_calibration_samples=20,
        max_brier_score=1.0,
    )

    assert first.status == "ready"
    assert first.training_sample_count == 96
    assert first.calibration_sample_count == 24
    assert first.training_end < first.calibration_start
    assert first.artifact_sha256 == second.artifact_sha256
    assert first.coefficients == second.coefficients

    calibrated = [point["calibrated"] for point in first.calibration_curve]
    assert calibrated == sorted(calibrated)


def test_prediction_returns_raw_and_calibrated_probabilities():
    artifact = train_probability_model(
        [sample(index) for index in range(120)],
        feature_names=("momentum_5d", "vwap_distance", "volatility_20d"),
        feature_version="1",
        min_training_samples=80,
        min_calibration_samples=20,
        max_brier_score=1.0,
    )

    result = predict_probability(
        artifact,
        {"momentum_5d": 0.05, "vwap_distance": 0.02, "volatility_20d": 0.09},
    )

    assert 0 <= result.raw_probability <= 1
    assert 0 <= result.calibrated_probability <= 1
    assert result.expected_net_return == pytest.approx(
        result.calibrated_probability * artifact.average_win
        + (1 - result.calibrated_probability) * artifact.average_loss
    )


def test_model_readiness_fails_closed_for_samples_brier_and_feature_version():
    insufficient = train_probability_model(
        [sample(index) for index in range(50)],
        feature_names=("momentum_5d", "vwap_distance", "volatility_20d"),
        feature_version="1",
        min_training_samples=80,
        min_calibration_samples=20,
        max_brier_score=0.25,
    )
    assert insufficient.status == "rejected"
    assert "training_samples" in insufficient.reasons

    ready = train_probability_model(
        [sample(index) for index in range(120)],
        feature_names=("momentum_5d", "vwap_distance", "volatility_20d"),
        feature_version="1",
        min_training_samples=80,
        min_calibration_samples=20,
        max_brier_score=1.0,
    )
    check = model_readiness(
        ready,
        feature_version="2",
        min_training_samples=80,
        min_calibration_samples=20,
        max_brier_score=0.00001,
    )

    assert check["ready"] is False
    assert "feature_version" in check["reasons"]
    assert "brier_score" in check["reasons"]

