from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Iterable


@dataclass(frozen=True)
class ModelSample:
    entry_at: datetime
    features: dict[str, float]
    profitable: bool
    net_return: float


@dataclass(frozen=True)
class WindowLabel:
    profitable: bool
    net_return: float
    net_pnl: float


@dataclass(frozen=True)
class TrainedProbabilityModel:
    status: str
    reasons: tuple[str, ...]
    feature_version: str
    feature_names: tuple[str, ...]
    training_sample_count: int
    calibration_sample_count: int
    training_start: datetime | None
    training_end: datetime | None
    calibration_start: datetime | None
    calibration_end: datetime | None
    coefficients: dict[str, Any]
    calibration_curve: list[dict[str, float]]
    brier_score: float | None
    average_win: float
    average_loss: float
    artifact_sha256: str


@dataclass(frozen=True)
class ProbabilityPrediction:
    raw_probability: float
    calibrated_probability: float
    expected_net_return: float


def build_window_label(
    *,
    entry_at: datetime,
    exit_at: datetime,
    entry_price: float,
    exit_price: float,
    quantity: int,
    commission_rate: float,
    min_commission: float,
    stamp_tax_rate: float,
    transfer_fee_rate: float,
    slippage_bps: float,
) -> WindowLabel:
    if (entry_at.hour, entry_at.minute) != (14, 40):
        raise ValueError("入场时间必须为14:40")
    if (exit_at.hour, exit_at.minute) != (10, 30):
        raise ValueError("退出时间必须为下一交易日10:30")
    if exit_at.date() <= entry_at.date():
        raise ValueError("退出必须位于下一交易日")
    if entry_price <= 0 or exit_price <= 0 or quantity <= 0 or quantity % 100:
        raise ValueError("价格或A股整数手数量无效")
    buy_fill = entry_price * (1 + slippage_bps / 10_000)
    sell_fill = exit_price * (1 - slippage_bps / 10_000)
    buy_notional = buy_fill * quantity
    sell_notional = sell_fill * quantity
    buy_commission = max(buy_notional * commission_rate, min_commission)
    sell_commission = max(sell_notional * commission_rate, min_commission)
    transfer_fee = (buy_notional + sell_notional) * transfer_fee_rate
    stamp_tax = sell_notional * stamp_tax_rate
    net_pnl = (
        sell_notional
        - sell_commission
        - stamp_tax
        - transfer_fee
        - buy_notional
        - buy_commission
    )
    total_cost = buy_notional + buy_commission + buy_notional * transfer_fee_rate
    net_return = net_pnl / total_cost
    return WindowLabel(net_pnl > 0, net_return, net_pnl)


def _sigmoid(value: float) -> float:
    value = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def _fit_logistic(
    samples: list[ModelSample], feature_names: tuple[str, ...]
) -> dict[str, Any]:
    means: list[float] = []
    scales: list[float] = []
    columns = [[float(item.features[name]) for item in samples] for name in feature_names]
    for values in columns:
        means.append(statistics.fmean(values))
        scale = statistics.pstdev(values)
        scales.append(scale if scale > 1e-12 else 1.0)
    matrix = [
        [
            (float(item.features[name]) - means[index]) / scales[index]
            for index, name in enumerate(feature_names)
        ]
        for item in samples
    ]
    labels = [1.0 if item.profitable else 0.0 for item in samples]
    weights = [0.0] * len(feature_names)
    positive_rate = min(max(statistics.fmean(labels), 1e-6), 1 - 1e-6)
    intercept = math.log(positive_rate / (1 - positive_rate))
    learning_rate = 0.08
    regularization = 0.001
    for _ in range(600):
        predictions = [
            _sigmoid(intercept + sum(weight * value for weight, value in zip(weights, row)))
            for row in matrix
        ]
        errors = [prediction - label for prediction, label in zip(predictions, labels)]
        intercept -= learning_rate * statistics.fmean(errors)
        for index in range(len(weights)):
            gradient = statistics.fmean(
                error * row[index] for error, row in zip(errors, matrix)
            ) + regularization * weights[index]
            weights[index] -= learning_rate * gradient
    return {
        "intercept": intercept,
        "weights": weights,
        "means": means,
        "scales": scales,
    }


def _raw_probability(
    coefficients: dict[str, Any], feature_names: tuple[str, ...], features: dict[str, float]
) -> float:
    values = [
        (float(features[name]) - coefficients["means"][index])
        / coefficients["scales"][index]
        for index, name in enumerate(feature_names)
    ]
    score = float(coefficients["intercept"]) + sum(
        float(weight) * value
        for weight, value in zip(coefficients["weights"], values)
    )
    return _sigmoid(score)


def _isotonic_curve(raw: list[float], labels: list[float]) -> list[dict[str, float]]:
    ordered = sorted(zip(raw, labels), key=lambda item: item[0])
    blocks: list[dict[str, Any]] = []
    for probability, label in ordered:
        blocks.append(
            {
                "raw_values": [probability],
                "sum": label,
                "count": 1,
            }
        )
        while len(blocks) >= 2:
            left = blocks[-2]["sum"] / blocks[-2]["count"]
            right = blocks[-1]["sum"] / blocks[-1]["count"]
            if left <= right:
                break
            merged = {
                "raw_values": blocks[-2]["raw_values"] + blocks[-1]["raw_values"],
                "sum": blocks[-2]["sum"] + blocks[-1]["sum"],
                "count": blocks[-2]["count"] + blocks[-1]["count"],
            }
            blocks[-2:] = [merged]
    curve: list[dict[str, float]] = []
    for block in blocks:
        calibrated = block["sum"] / block["count"]
        for probability in block["raw_values"]:
            curve.append({"raw": probability, "calibrated": calibrated})
    return sorted(curve, key=lambda point: point["raw"])


def _calibrate(curve: list[dict[str, float]], raw: float) -> float:
    if not curve:
        return raw
    if raw <= curve[0]["raw"]:
        return curve[0]["calibrated"]
    if raw >= curve[-1]["raw"]:
        return curve[-1]["calibrated"]
    for left, right in zip(curve, curve[1:]):
        if left["raw"] <= raw <= right["raw"]:
            if right["raw"] == left["raw"]:
                return right["calibrated"]
            ratio = (raw - left["raw"]) / (right["raw"] - left["raw"])
            return left["calibrated"] + ratio * (
                right["calibrated"] - left["calibrated"]
            )
    return raw


def model_readiness(
    artifact: TrainedProbabilityModel,
    *,
    feature_version: str,
    min_training_samples: int,
    min_calibration_samples: int,
    max_brier_score: float,
) -> dict[str, Any]:
    reasons: list[str] = []
    if artifact.training_sample_count < min_training_samples:
        reasons.append("training_samples")
    if artifact.calibration_sample_count < min_calibration_samples:
        reasons.append("calibration_samples")
    if artifact.brier_score is None or artifact.brier_score > max_brier_score:
        reasons.append("brier_score")
    if artifact.feature_version != feature_version:
        reasons.append("feature_version")
    if artifact.status != "ready":
        reasons.extend(reason for reason in artifact.reasons if reason not in reasons)
    return {"ready": not reasons, "reasons": reasons}


def train_probability_model(
    samples: Iterable[ModelSample],
    *,
    feature_names: tuple[str, ...],
    feature_version: str,
    min_training_samples: int,
    min_calibration_samples: int,
    max_brier_score: float,
) -> TrainedProbabilityModel:
    rows = sorted(samples, key=lambda item: item.entry_at)
    split = max(1, int(len(rows) * 0.8)) if rows else 0
    training = rows[:split]
    calibration = rows[split:]
    reasons: list[str] = []
    if len(training) < min_training_samples:
        reasons.append("training_samples")
    if len(calibration) < min_calibration_samples:
        reasons.append("calibration_samples")
    if not training or len({item.profitable for item in training}) < 2:
        reasons.append("training_labels")

    coefficients: dict[str, Any] = {}
    curve: list[dict[str, float]] = []
    brier_score: float | None = None
    if training and len({item.profitable for item in training}) >= 2:
        coefficients = _fit_logistic(training, feature_names)
        raw = [_raw_probability(coefficients, feature_names, item.features) for item in calibration]
        labels = [1.0 if item.profitable else 0.0 for item in calibration]
        curve = _isotonic_curve(raw, labels) if calibration else []
        calibrated = [_calibrate(curve, value) for value in raw]
        if labels:
            brier_score = statistics.fmean(
                (prediction - label) ** 2
                for prediction, label in zip(calibrated, labels)
            )
    if brier_score is None or brier_score > max_brier_score:
        reasons.append("brier_score")

    wins = [item.net_return for item in rows if item.net_return > 0]
    losses = [item.net_return for item in rows if item.net_return <= 0]
    payload = {
        "feature_version": feature_version,
        "feature_names": feature_names,
        "training_start": training[0].entry_at.isoformat() if training else None,
        "training_end": training[-1].entry_at.isoformat() if training else None,
        "calibration_start": calibration[0].entry_at.isoformat() if calibration else None,
        "calibration_end": calibration[-1].entry_at.isoformat() if calibration else None,
        "coefficients": coefficients,
        "calibration_curve": curve,
        "average_win": statistics.fmean(wins) if wins else 0.0,
        "average_loss": statistics.fmean(losses) if losses else 0.0,
    }
    artifact_sha256 = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    unique_reasons = tuple(dict.fromkeys(reasons))
    return TrainedProbabilityModel(
        status="ready" if not unique_reasons else "rejected",
        reasons=unique_reasons,
        feature_version=feature_version,
        feature_names=feature_names,
        training_sample_count=len(training),
        calibration_sample_count=len(calibration),
        training_start=training[0].entry_at if training else None,
        training_end=training[-1].entry_at if training else None,
        calibration_start=calibration[0].entry_at if calibration else None,
        calibration_end=calibration[-1].entry_at if calibration else None,
        coefficients=coefficients,
        calibration_curve=curve,
        brier_score=brier_score,
        average_win=payload["average_win"],
        average_loss=payload["average_loss"],
        artifact_sha256=artifact_sha256,
    )


def predict_probability(
    artifact: TrainedProbabilityModel,
    features: dict[str, float],
) -> ProbabilityPrediction:
    if artifact.status != "ready":
        raise ValueError("概率模型尚未就绪")
    missing = [name for name in artifact.feature_names if name not in features]
    if missing:
        raise ValueError(f"概率特征缺失: {', '.join(missing)}")
    raw = _raw_probability(artifact.coefficients, artifact.feature_names, features)
    calibrated = _calibrate(artifact.calibration_curve, raw)
    expected = (
        calibrated * artifact.average_win
        + (1 - calibrated) * artifact.average_loss
    )
    return ProbabilityPrediction(raw, calibrated, expected)
