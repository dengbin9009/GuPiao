from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ProbabilityCandidateDecision,
    ProbabilityPortfolioRun,
    ProbabilityTrainingSample,
    SimulationAccount,
    Stock,
    StrategyConfig,
    StrategyRun,
)
from .allocation import plan_buy_quantity
from .config import PROBABILITY_PORTFOLIO_DEFAULTS
from .execution import RejectedCandidate, ScoredCandidate, execute_portfolio_entry
from .features import FEATURE_NAMES
from .model import build_window_label


OBSERVATION_TRIGGER = "portfolio_observation"
STALE_EVENT_REASON = "公司事件数据未就绪或已过期"


def _next_weekday(value):
    result = value + timedelta(days=1)
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result


def _observation_features(
    features: dict[str, float] | None,
    *,
    entry_price: float,
    quantity: int,
    account: SimulationAccount,
) -> dict[str, float]:
    return {
        **(features or {}),
        "_entry_price": float(entry_price),
        "_label_quantity": quantity,
        "_commission_rate": float(account.commission_rate),
        "_min_commission": float(account.min_commission),
        "_stamp_tax_rate": float(account.stamp_tax_rate),
        "_transfer_fee_rate": float(account.transfer_fee_rate),
        "_slippage_bps": float(account.slippage_bps),
    }


def _label_quantity(account: SimulationAccount, entry_price: float) -> int:
    return plan_buy_quantity(
        total_asset=float(account.total_asset),
        available_cash=float(account.available_cash),
        target_weight=float(PROBABILITY_PORTFOLIO_DEFAULTS["min_position_pct"]),
        market_price=entry_price,
        slippage_bps=float(account.slippage_bps),
        commission_rate=float(account.commission_rate),
        min_commission=float(account.min_commission),
        transfer_fee_rate=float(account.transfer_fee_rate),
    ).quantity


def _training_exit_due(
    planned: date,
    current: date,
    *,
    calendar: Any | None,
) -> bool:
    if calendar is None:
        return planned == current
    if planned > current:
        return False
    trading_days = sorted(calendar.trading_days(start=planned, end=current))
    return bool(trading_days) and trading_days[0] == current.isoformat()


def _training_eligible(decision: ProbabilityCandidateDecision) -> bool:
    return (
        set(FEATURE_NAMES) <= set(decision.features or {})
        and STALE_EVENT_REASON not in (decision.rejection_reasons or [])
    )


def record_probability_observation(
    db: Session,
    config: StrategyConfig,
    *,
    current: datetime,
    scored_candidates: list[ScoredCandidate],
    rejected_candidates: list[RejectedCandidate],
    candidate_reasons: tuple[str, ...],
) -> StrategyRun:
    exit_date = _next_weekday(current.date())
    account = db.get(SimulationAccount, config.simulation_account_id)
    if account is None:
        raise ValueError("概率组合训练观察缺少模拟账户")
    enriched_scored: list[ScoredCandidate] = []
    for item in scored_candidates:
        stock = db.get(Stock, item.stock_id)
        entry_price = float(stock.last_price or 0) if stock else 0
        enriched_scored.append(
            replace(
                item,
                features=_observation_features(
                    item.features,
                    entry_price=entry_price,
                    quantity=_label_quantity(account, entry_price),
                    account=account,
                ),
            )
        )
    enriched_rejected: list[RejectedCandidate] = []
    for item in rejected_candidates:
        stock = db.get(Stock, item.stock_id)
        entry_price = float(stock.last_price or 0) if stock else 0
        enriched_rejected.append(
            replace(
                item,
                features=_observation_features(
                    item.features,
                    entry_price=entry_price,
                    quantity=_label_quantity(account, entry_price),
                    account=account,
                ),
            )
        )
    return execute_portfolio_entry(
        db,
        config,
        current=current,
        scored_candidates=enriched_scored,
        rejected_candidates=enriched_rejected,
        candidate_reasons=candidate_reasons,
        trigger_type=OBSERVATION_TRIGGER,
        dry_run=True,
        summary_context={"training_exit_date": exit_date.isoformat()},
    )


def pending_observation_symbols(
    db: Session,
    config: StrategyConfig,
    *,
    current: datetime,
    calendar: Any | None = None,
) -> list[str]:
    runs = list(
        db.scalars(
            select(ProbabilityPortfolioRun).where(
                ProbabilityPortfolioRun.strategy_config_id == config.id,
                ProbabilityPortfolioRun.trigger_type == OBSERVATION_TRIGGER,
                ProbabilityPortfolioRun.status == "completed",
                ProbabilityPortfolioRun.trading_date < current.date().isoformat(),
            )
        )
    )
    stock_ids: set[int] = set()
    for run in runs:
        strategy_run = db.get(StrategyRun, run.strategy_run_id)
        try:
            planned = date.fromisoformat(
                (strategy_run.summary or {}).get("training_exit_date", "")
            )
        except ValueError:
            continue
        if not _training_exit_due(planned, current.date(), calendar=calendar):
            continue
        for decision in db.scalars(
            select(ProbabilityCandidateDecision).where(
                ProbabilityCandidateDecision.portfolio_run_id == run.id
            )
        ):
            if _training_eligible(decision):
                stock_ids.add(decision.stock_id)
    if not stock_ids:
        return []
    return list(
        db.scalars(
            select(Stock.symbol)
            .where(Stock.id.in_(stock_ids))
            .order_by(Stock.symbol)
        )
    )


def finalize_probability_training_samples(
    db: Session,
    config: StrategyConfig,
    *,
    current: datetime,
    calendar: Any | None = None,
) -> dict[str, int]:
    parameters = {**PROBABILITY_PORTFOLIO_DEFAULTS, **(config.parameters or {})}
    account = db.get(SimulationAccount, config.simulation_account_id)
    if account is None:
        return {"created": 0, "skipped": 0, "errors": 1}
    created = 0
    skipped = 0
    errors = 0
    runs = list(
        db.scalars(
            select(ProbabilityPortfolioRun).where(
                ProbabilityPortfolioRun.strategy_config_id == config.id,
                ProbabilityPortfolioRun.trigger_type == OBSERVATION_TRIGGER,
                ProbabilityPortfolioRun.status == "completed",
                ProbabilityPortfolioRun.trading_date < current.date().isoformat(),
            )
        )
    )
    for run in runs:
        strategy_run = db.get(StrategyRun, run.strategy_run_id)
        if strategy_run is None:
            continue
        summary = strategy_run.summary or {}
        try:
            planned = date.fromisoformat(summary.get("training_exit_date", ""))
        except ValueError:
            continue
        if not _training_exit_due(planned, current.date(), calendar=calendar):
            continue
        entry_at = strategy_run.started_at
        if entry_at.tzinfo is None:
            entry_at = entry_at.replace(tzinfo=current.tzinfo)
        decisions = list(
            db.scalars(
                select(ProbabilityCandidateDecision).where(
                    ProbabilityCandidateDecision.portfolio_run_id == run.id
                )
            )
        )
        for decision in decisions:
            features = decision.features or {}
            if not _training_eligible(decision):
                continue
            stock = db.get(Stock, decision.stock_id)
            quote_at = stock.quote_updated_at if stock else None
            if quote_at is not None and quote_at.tzinfo is None:
                quote_at = quote_at.replace(tzinfo=current.tzinfo)
            if (
                stock is None
                or not stock.last_price
                or quote_at is None
                or (current - quote_at).total_seconds() < 0
                or (current - quote_at).total_seconds() > 60
            ):
                errors += 1
                continue
            try:
                entry_price = float(features["_entry_price"])
                quantity = int(features["_label_quantity"])
                commission_rate = float(features["_commission_rate"])
                min_commission = float(features["_min_commission"])
                stamp_tax_rate = float(features["_stamp_tax_rate"])
                transfer_fee_rate = float(features["_transfer_fee_rate"])
                slippage_bps = float(features["_slippage_bps"])
                clean_features = {
                    name: float(features[name]) for name in FEATURE_NAMES
                }
                frozen_values = [
                    entry_price,
                    commission_rate,
                    min_commission,
                    stamp_tax_rate,
                    transfer_fee_rate,
                    slippage_bps,
                    *clean_features.values(),
                ]
                if quantity <= 0 or quantity % 100 or not all(
                    math.isfinite(value) for value in frozen_values
                ):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                errors += 1
                continue
            exit_at = datetime.combine(
                current.date(),
                time(10, 30),
                tzinfo=current.tzinfo,
            )
            label = build_window_label(
                entry_at=entry_at,
                exit_at=exit_at,
                entry_price=entry_price,
                exit_price=float(stock.last_price),
                quantity=quantity,
                commission_rate=commission_rate,
                min_commission=min_commission,
                stamp_tax_rate=stamp_tax_rate,
                transfer_fee_rate=transfer_fee_rate,
                slippage_bps=slippage_bps,
            )
            payload = {
                "stock_id": stock.id,
                "entry_at": entry_at.isoformat(),
                "exit_at": exit_at.isoformat(),
                "features": clean_features,
                "entry_price": entry_price,
                "exit_price": float(stock.last_price),
                "quantity": quantity,
                "costs": {
                    "commission_rate": commission_rate,
                    "min_commission": min_commission,
                    "stamp_tax_rate": stamp_tax_rate,
                    "transfer_fee_rate": transfer_fee_rate,
                    "slippage_bps": slippage_bps,
                },
            }
            source_sha256 = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            existing = db.scalar(
                select(ProbabilityTrainingSample.id).where(
                    ProbabilityTrainingSample.stock_id == stock.id,
                    ProbabilityTrainingSample.entry_at == entry_at,
                    ProbabilityTrainingSample.feature_version
                    == str(parameters["feature_version"]),
                )
            )
            if existing is not None:
                skipped += 1
                continue
            db.add(
                ProbabilityTrainingSample(
                    stock_id=stock.id,
                    entry_at=entry_at,
                    exit_at=exit_at,
                    feature_version=str(parameters["feature_version"]),
                    features=clean_features,
                    net_return=label.net_return,
                    profitable=label.profitable,
                    source_sha256=source_sha256,
                )
            )
            created += 1
    db.commit()
    return {"created": created, "skipped": skipped, "errors": errors}
