from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    DataSourceState,
    MarketDailyBar,
    ProbabilityModelArtifact,
    Stock,
    StockEvent,
    StrategyConfig,
)
from .execution import RejectedCandidate, ScoredCandidate
from .features import build_feature_vector
from .model import TrainedProbabilityModel, predict_probability
from .readiness import latest_qualified_artifact


BLOCKING_EVENT_TYPES = {
    "suspension",
    "resumption",
    "regulatory_investigation",
    "material_litigation",
    "shareholder_reduction",
    "earnings_warning",
    "major_announcement",
}


@dataclass(frozen=True)
class CandidateBuildResult:
    scored: list[ScoredCandidate]
    rejected: list[RejectedCandidate]
    reasons: tuple[str, ...]
    model_artifact_id: int | None


def _artifact_model(row: ProbabilityModelArtifact) -> TrainedProbabilityModel:
    value = row.coefficients or {}
    model = value.get("model")
    names = tuple(value.get("feature_names") or ())
    if not isinstance(model, dict) or not names:
        raise ValueError("概率模型产物契约无效")
    return TrainedProbabilityModel(
        status=row.status,
        reasons=(),
        feature_version=row.feature_version,
        feature_names=names,
        training_sample_count=row.training_sample_count,
        calibration_sample_count=row.calibration_sample_count,
        training_start=None,
        training_end=None,
        calibration_start=None,
        calibration_end=None,
        coefficients=model,
        calibration_curve=list(row.calibration_curve or []),
        brier_score=row.brier_score,
        average_win=float(value.get("average_win", 0)),
        average_loss=float(value.get("average_loss", 0)),
        artifact_sha256=row.artifact_sha256 or "",
    )


def build_scored_candidates(
    db: Session,
    config: StrategyConfig,
    *,
    current: datetime,
) -> CandidateBuildResult:
    parameters = config.parameters or {}
    artifact = latest_qualified_artifact(
        db,
        parameters,
        current=current,
    )
    model = None
    global_reasons: list[str] = []
    if artifact is None:
        global_reasons.append("概率模型尚未就绪")
    else:
        try:
            model = _artifact_model(artifact)
        except ValueError as exc:
            global_reasons.append(str(exc))

    sources = list(
        db.scalars(
            select(DataSourceState)
            .where(
                DataSourceState.enabled.is_(True),
                DataSourceState.healthy.is_(True),
            )
            .order_by(DataSourceState.last_checked_at.desc())
        )
    )
    source = next(
        (item for item in sources if "realtime" in (item.capabilities or [])),
        None,
    )
    source_healthy = source is not None
    event_source = next(
        (
            item
            for item in sources
            if "corporate_events" in (item.capabilities or [])
        ),
        None,
    )
    event_checked_at = event_source.last_checked_at if event_source else None
    if event_checked_at is not None and event_checked_at.tzinfo is None:
        event_checked_at = event_checked_at.replace(tzinfo=current.tzinfo)
    event_max_age = min(
        1800,
        max(1, int(parameters.get("event_max_age_seconds", 1800))),
    )
    if (
        event_checked_at is None
        or (current - event_checked_at).total_seconds() < 0
        or (current - event_checked_at).total_seconds() > event_max_age
    ):
        global_reasons.append("公司事件数据未就绪或已过期")
    prefilter_size = max(1, min(100, int(parameters.get("prefilter_size", 100))))
    stocks = list(
        db.scalars(
            select(Stock)
            .where(
                Stock.status == "active",
                Stock.exchange.in_(["SSE", "SZSE"]),
            )
            .order_by(func.coalesce(Stock.turnover_amount, 0).desc(), Stock.symbol)
            .limit(prefilter_size)
        )
    )
    benchmark = db.scalar(select(Stock).where(Stock.symbol == "000300.SH"))
    benchmark_bars = (
        list(
            db.scalars(
                select(MarketDailyBar)
                .where(
                    MarketDailyBar.stock_id == benchmark.id,
                    MarketDailyBar.trade_date < current.date().isoformat(),
                )
                .order_by(MarketDailyBar.trade_date.desc())
                .limit(20)
            )
        )
        if benchmark
        else []
    )
    benchmark_bars.reverse()
    market_breadth = (
        sum(1 for item in stocks if float(item.change_pct or 0) > 0) / len(stocks)
        if stocks
        else 0.0
    )
    event_rows = list(
        db.execute(
            select(StockEvent.stock_id, StockEvent.event_type, StockEvent.unlock_free_float_pct)
            .where(
                StockEvent.published_at >= current - timedelta(days=7),
                StockEvent.published_at <= current,
            )
        )
    )
    critical_ids = {
        stock_id
        for stock_id, event_type, unlock_pct in event_rows
        if event_type in BLOCKING_EVENT_TYPES
        or (event_type == "unlock" and (unlock_pct is None or unlock_pct > 0.05))
    }
    scored: list[ScoredCandidate] = []
    rejected: list[RejectedCandidate] = []
    for stock in stocks:
        bars = list(
            db.scalars(
                select(MarketDailyBar)
                .where(MarketDailyBar.stock_id == stock.id)
                .order_by(MarketDailyBar.trade_date.desc())
                .limit(21)
            )
        )
        bars.reverse()
        result = build_feature_vector(
            stock,
            bars,
            benchmark_bars,
            current=current,
            source_healthy=source_healthy,
            critical_event=stock.id in critical_ids,
            market_breadth=market_breadth,
            max_quote_age_seconds=int(parameters.get("quote_max_age_seconds", 60)),
        )
        reasons = tuple(dict.fromkeys([*global_reasons, *result.reasons]))
        if reasons:
            rejected.append(
                RejectedCandidate(
                    stock.id,
                    stock.symbol,
                    reasons,
                    result.features,
                )
            )
            continue
        if model is None:
            rejected.append(
                RejectedCandidate(stock.id, stock.symbol, ("概率模型尚未就绪",))
            )
            continue
        try:
            prediction = predict_probability(model, result.features)
        except ValueError as exc:
            rejected.append(RejectedCandidate(stock.id, stock.symbol, (str(exc),)))
            continue
        scored.append(
            ScoredCandidate(
                stock_id=stock.id,
                symbol=stock.symbol,
                features=result.features,
                raw_probability=prediction.raw_probability,
                calibrated_probability=prediction.calibrated_probability,
                expected_net_return=prediction.expected_net_return,
                volatility_20d=result.features["volatility_20d"],
            )
        )
    scored.sort(
        key=lambda item: (
            -item.calibrated_probability,
            -item.expected_net_return,
            item.symbol,
        )
    )
    return CandidateBuildResult(
        scored,
        rejected,
        tuple(dict.fromkeys(global_reasons)),
        artifact.id if artifact else None,
    )
