from __future__ import annotations

from typing import Any


FEATURE_VERSION = "1"

PROBABILITY_PORTFOLIO_DEFAULTS: dict[str, Any] = {
    "timezone": "Asia/Shanghai",
    "entry_time": "14:40",
    "exit_time": "10:30",
    "latest_exit_time": "10:45",
    "retry_seconds": 15,
    "max_positions": 10,
    "min_probability": 0.55,
    "min_expected_net_return": 0.0,
    "min_position_pct": 0.02,
    "max_position_pct": 0.36,
    "min_total_exposure_pct": 0.30,
    "max_total_exposure_pct": 0.60,
    "volatility_floor": 0.01,
    "daily_loss_limit_pct": 0.015,
    "min_training_samples": 500,
    "min_calibration_samples": 100,
    "max_brier_score": 0.25,
    "feature_version": FEATURE_VERSION,
    "dry_run": True,
}
