from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    DataSourceState,
    FinancialReportSnapshot,
    MarketDailyBar,
    MarketDailyMetric,
    Stock,
    StrategyBacktestQualification,
    StrategyConfig,
    StrategyDefinition,
    StrategyDryRunApproval,
    RiskSettings,
    StrategyRiskProfile,
    StrategySchedule,
    now,
)
from .catalog import DEFAULT_ETF_UNIVERSE, QUANT_STRATEGY_SPECS


QUANT_DATASET_STATES = {
    "stock_daily": ("quant_stock_daily", "股票日线与复权批次"),
    "daily_metric": ("quant_daily_metric", "每日估值批次"),
    "financial": ("quant_financial", "点时财务批次"),
    "etf_daily": ("quant_etf_daily", "ETF日线批次"),
    "benchmark_daily": ("quant_benchmark_daily", "基准指数日线批次"),
}


def quant_dataset_state_reasons(
    db: Session,
    key: str,
    *,
    as_of: date,
) -> list[str]:
    spec = QUANT_STRATEGY_SPECS[key]
    names = ["etf_daily" if spec.asset_type == "ETF" else "stock_daily"]
    if "daily_metric" in spec.required_datasets:
        names.append("daily_metric")
    if "financial" in spec.required_datasets:
        names.append("financial")
    if key == "short_term_reversal_t1":
        names.append("benchmark_daily")
    reasons: list[str] = []
    for name in names:
        provider, label = QUANT_DATASET_STATES[name]
        state = db.scalar(
            select(DataSourceState).where(DataSourceState.provider == provider)
        )
        if state is None or not state.healthy or state.last_checked_at is None:
            detail = state.last_error if state and state.last_error else "尚未完成"
            reasons.append(f"{label}{detail}")
            continue
        checked_at = state.last_checked_at
        if checked_at.date() != as_of:
            reasons.append(f"{label}尚未更新到{as_of.isoformat()}")
    return reasons


def corporate_event_data_reason(
    db: Session,
    *,
    current: datetime,
) -> str | None:
    sources = [
        source
        for source in db.scalars(
            select(DataSourceState).where(DataSourceState.enabled.is_(True))
        )
        if "corporate_events" in (source.capabilities or [])
        and source.healthy
        and source.last_checked_at is not None
    ]
    if not sources:
        return "风险公告数据源尚未就绪"
    source = max(sources, key=lambda item: item.last_checked_at)
    checked_at = source.last_checked_at
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=current.tzinfo)
    age_seconds = (current - checked_at).total_seconds()
    if age_seconds < 0 or age_seconds > source.stale_after_seconds:
        return "风险公告数据已过期"
    return None


def configuration_fingerprint(
    parameters: dict[str, Any],
    *,
    simulation_account_id: int | None,
    strategy_version: str,
) -> str:
    value = {
        "parameters": parameters,
        "simulation_account_id": simulation_account_id,
        "strategy_version": strategy_version,
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _data_reasons(
    db: Session,
    key: str,
    parameters: dict[str, Any],
    *,
    current: datetime | None = None,
) -> list[str]:
    spec = QUANT_STRATEGY_SPECS[key]
    reasons: list[str] = []
    minimum_days = 500
    valid_bar_filter = (
        MarketDailyBar.quality_status == "valid",
        MarketDailyBar.adjusted_close.is_not(None),
        MarketDailyBar.adjustment_factor.is_not(None),
        MarketDailyBar.adjustment_factor > 0,
        ~func.lower(MarketDailyBar.source).like("%demo%"),
    )
    latest_trade_date = db.scalar(
        select(func.max(MarketDailyBar.trade_date))
        .join(Stock, Stock.id == MarketDailyBar.stock_id)
        .where(
            Stock.instrument_type == spec.asset_type,
            *valid_bar_filter,
        )
    )
    day_count = 0
    if latest_trade_date:
        day_count = db.scalar(
            select(func.count(func.distinct(MarketDailyBar.trade_date)))
            .join(Stock, Stock.id == MarketDailyBar.stock_id)
            .where(
                Stock.instrument_type == spec.asset_type,
                *valid_bar_filter,
            )
        ) or 0
    if spec.asset_type == "ETF":
        symbols = list(parameters.get("etf_universe") or DEFAULT_ETF_UNIVERSE)
        counts = {
            symbol: int(count)
            for symbol, count in db.execute(
                select(
                    Stock.symbol,
                    func.count(func.distinct(MarketDailyBar.trade_date)),
                )
                .join(MarketDailyBar, MarketDailyBar.stock_id == Stock.id)
                .where(
                    Stock.symbol.in_(symbols),
                    Stock.instrument_type == "ETF",
                    *valid_bar_filter,
                )
                .group_by(Stock.symbol)
            )
        }
        if any(counts.get(symbol, 0) < minimum_days for symbol in symbols):
            reasons.append("配置 ETF 逐只历史不足500个交易日")
    elif day_count < minimum_days:
        reasons.append(f"真实复权日线不足{minimum_days}个交易日")
    if "daily_metric" in spec.required_datasets:
        latest_metric_date = db.scalar(
            select(func.max(MarketDailyMetric.trade_date))
        )
        if latest_metric_date is None:
            reasons.append("每日估值指标尚未就绪")
        elif latest_trade_date and latest_metric_date < latest_trade_date:
            reasons.append("每日估值指标尚未更新到最新交易日")
    if "financial" in spec.required_datasets:
        report_conditions = []
        if latest_trade_date:
            report_conditions.extend(
                [
                    FinancialReportSnapshot.available_on <= latest_trade_date,
                    FinancialReportSnapshot.actual_announcement_date
                    <= latest_trade_date,
                ]
            )
        report_count = db.scalar(
            select(func.count())
            .select_from(FinancialReportSnapshot)
            .where(*report_conditions)
        ) or 0
        if report_count == 0:
            reasons.append("点时财务快照尚未就绪")
        elif latest_trade_date:
            latest_report = db.scalar(
                select(func.max(FinancialReportSnapshot.available_on))
                .where(*report_conditions)
            )
            if latest_report and (
                date.fromisoformat(latest_trade_date)
                - date.fromisoformat(latest_report)
            ).days > 200:
                reasons.append("点时财务快照已过期")
    if "events" in spec.required_datasets:
        event_reason = corporate_event_data_reason(
            db,
            current=current or now(),
        )
        if event_reason:
            reasons.append(event_reason)
    if latest_trade_date:
        reasons.extend(
            quant_dataset_state_reasons(
                db,
                key,
                as_of=date.fromisoformat(latest_trade_date),
            )
        )
    return reasons


def quant_strategy_readiness(db: Session, config_id: int) -> dict[str, Any]:
    config = db.get(StrategyConfig, config_id)
    if config is None:
        raise ValueError("独立量化策略配置不存在")
    definition = db.get(StrategyDefinition, config.strategy_definition_id)
    if definition is None or definition.key not in QUANT_STRATEGY_SPECS:
        raise ValueError("配置不属于独立量化策略")
    risk = db.scalar(
        select(StrategyRiskProfile).where(
            StrategyRiskProfile.strategy_config_id == config.id
        )
    )
    fingerprint = configuration_fingerprint(
        config.parameters or {},
        simulation_account_id=config.simulation_account_id,
        strategy_version=definition.version,
    )
    data_version = str((config.parameters or {}).get("data_version", "1"))
    data_reasons = _data_reasons(
        db,
        definition.key,
        config.parameters or {},
    )
    qualification = db.scalar(
        select(StrategyBacktestQualification)
        .where(
            StrategyBacktestQualification.strategy_config_id == config.id,
            StrategyBacktestQualification.config_fingerprint == fingerprint,
            StrategyBacktestQualification.strategy_version == definition.version,
            StrategyBacktestQualification.data_version == data_version,
            StrategyBacktestQualification.qualified.is_(True),
        )
        .order_by(StrategyBacktestQualification.id.desc())
        .limit(1)
    )
    approval = db.scalar(
        select(StrategyDryRunApproval)
        .where(
            StrategyDryRunApproval.strategy_config_id == config.id,
            StrategyDryRunApproval.config_fingerprint == fingerprint,
            StrategyDryRunApproval.strategy_version == definition.version,
            StrategyDryRunApproval.data_version == data_version,
        )
        .order_by(StrategyDryRunApproval.id.desc())
        .limit(1)
    )
    schedules = list(
        db.scalars(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id
            )
        )
    )
    schedule_enabled = {item.trigger_type: item.enabled for item in schedules}
    reasons = list(data_reasons)
    if qualification is None:
        reasons.append("当前配置尚无合格回测")
    if approval is None:
        reasons.append("当前配置尚未完成无下单演练")
    if config.mode != "SIMULATION" or config.simulation_account_id is None:
        reasons.append("策略必须绑定独立模拟账户")

    failed = bool(risk and risk.consecutive_errors >= risk.max_consecutive_errors)
    system_risk = db.scalar(
        select(RiskSettings).where(RiskSettings.mode == "SIMULATION")
    )
    system_paused = bool(system_risk and system_risk.emergency_stop_enabled)
    if system_paused:
        reasons.append("系统级紧急停止已启用")
    paused = bool((risk and risk.emergency_stop_enabled) or system_paused)
    gates_ready = not reasons
    active = bool(
        gates_ready
        and schedule_enabled.get("quant_signal")
        and schedule_enabled.get("quant_execute")
    )
    if failed:
        status = "FAILED"
    elif paused:
        status = "PAUSED"
    elif data_reasons:
        status = "DATA_PENDING"
    elif qualification is None:
        status = "BACKTEST_PENDING"
    elif approval is None:
        status = "DRY_RUN_PENDING"
    elif active:
        status = "ACTIVE"
    else:
        status = "READY"
    return {
        "strategy_key": definition.key,
        "strategy_config_id": config.id,
        "simulation_account_id": config.simulation_account_id,
        "simulation_only": True,
        "status": status,
        "ready": gates_ready,
        "automation_ready": gates_ready and not failed and not paused,
        "reasons": list(dict.fromkeys(reasons)),
        "config_fingerprint": fingerprint,
        "data_version": data_version,
        "backtest_qualified": qualification is not None,
        "dry_run_validated": approval is not None,
        "signal_schedule_enabled": bool(schedule_enabled.get("quant_signal")),
        "execution_schedule_enabled": bool(schedule_enabled.get("quant_execute")),
    }


def record_dry_run_approval(
    db: Session,
    config: StrategyConfig,
    decision,
) -> StrategyDryRunApproval:
    existing = db.scalar(
        select(StrategyDryRunApproval).where(
            StrategyDryRunApproval.decision_id == decision.id
        )
    )
    if existing:
        return existing
    row = StrategyDryRunApproval(
        strategy_config_id=config.id,
        decision_id=decision.id,
        config_fingerprint=decision.config_fingerprint,
        strategy_version=decision.strategy_version,
        data_version=decision.data_version,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
