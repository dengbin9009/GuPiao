from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    MarketDailyBar,
    Position,
    SimulationAccount,
    Stock,
    StrategyConfig,
    StrategyDefinition,
    StrategyPerformanceDaily,
    StrategyRiskProfile,
    StrategySchedule,
)
from ..notifications import queue_notifications
from .catalog import QUANT_STRATEGY_SPECS


def _close_valuation(
    db: Session,
    account: SimulationAccount,
    *,
    trading_date: str,
) -> tuple[float, float] | None:
    market_value = 0.0
    rows = list(
        db.execute(
            select(Position, Stock)
            .join(Stock, Stock.id == Position.stock_id)
            .where(
                Position.account_id == account.id,
                Position.mode == "SIMULATION",
                Position.quantity > 0,
            )
        )
    )
    for position, stock in rows:
        bar = db.scalar(
            select(MarketDailyBar).where(
                MarketDailyBar.stock_id == stock.id,
                MarketDailyBar.trade_date == trading_date,
                MarketDailyBar.quality_status == "valid",
            )
        )
        if bar is None or float(bar.close) <= 0:
            return None
        position.market_value = int(position.quantity) * float(bar.close)
        position.unrealized_pnl = (
            float(bar.close) - float(position.average_cost)
        ) * int(position.quantity)
        market_value += float(position.market_value)
    total_asset = float(account.cash_balance) + market_value
    account.total_asset = total_asset
    account.unrealized_pnl = sum(
        float(position.unrealized_pnl)
        for position, _stock in rows
    )
    return market_value, total_asset


def record_quant_daily_performance(
    db: Session,
    *,
    current: datetime,
) -> dict[str, int]:
    trading_date = current.date().isoformat()
    configs = list(
        db.scalars(
            select(StrategyConfig)
            .join(StrategyDefinition)
            .where(
                StrategyDefinition.key.in_(tuple(QUANT_STRATEGY_SPECS)),
                StrategyConfig.mode == "SIMULATION",
            )
            .order_by(StrategyConfig.id)
        )
    )
    recorded = 0
    skipped = 0
    paused = 0
    for config in configs:
        existing = db.scalar(
            select(StrategyPerformanceDaily).where(
                StrategyPerformanceDaily.strategy_config_id == config.id,
                StrategyPerformanceDaily.trading_date == trading_date,
            )
        )
        if existing is not None:
            captured_at = existing.captured_at
            if captured_at.tzinfo is None:
                captured_at = captured_at.replace(tzinfo=current.tzinfo)
            if captured_at.hour >= 15:
                skipped += 1
                continue
        account = db.get(SimulationAccount, config.simulation_account_id)
        risk = db.scalar(
            select(StrategyRiskProfile).where(
                StrategyRiskProfile.strategy_config_id == config.id
            )
        )
        if account is None or risk is None:
            skipped += 1
            continue
        valuation = _close_valuation(
            db,
            account,
            trading_date=trading_date,
        )
        if valuation is None:
            skipped += 1
            continue
        market_value, total_asset = valuation
        previous = db.scalar(
            select(StrategyPerformanceDaily)
            .where(
                StrategyPerformanceDaily.strategy_config_id == config.id,
                StrategyPerformanceDaily.trading_date < trading_date,
            )
            .order_by(StrategyPerformanceDaily.trading_date.desc())
            .limit(1)
        )
        opening_asset = (
            float(previous.total_asset)
            if previous and previous.total_asset > 0
            else float(account.initial_cash)
        )
        historical_peak = db.scalar(
            select(func.max(StrategyPerformanceDaily.total_asset)).where(
                StrategyPerformanceDaily.strategy_config_id == config.id
            )
        )
        peak = max(float(historical_peak or 0), float(account.initial_cash))
        daily_return = total_asset / opening_asset - 1 if opening_asset else 0
        drawdown = total_asset / peak - 1 if peak else 0
        row = existing
        if row is None:
            row = StrategyPerformanceDaily(
                strategy_config_id=config.id,
                simulation_account_id=account.id,
                trading_date=trading_date,
            )
            db.add(row)
        row.cash_balance = account.cash_balance
        row.market_value = market_value
        row.total_asset = total_asset
        row.daily_return = daily_return
        row.cumulative_return = total_asset / account.initial_cash - 1
        row.drawdown = drawdown
        row.exposure = market_value / total_asset if total_asset else 0
        row.captured_at = current
        breached = (
            daily_return <= -abs(risk.daily_loss_limit_pct)
            or drawdown <= -abs(risk.max_drawdown_pct)
        )
        if breached and not risk.emergency_stop_enabled:
            risk.emergency_stop_enabled = True
            for schedule in db.scalars(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id == config.id
                )
            ):
                schedule.enabled = False
                schedule.next_run_at = None
            paused += 1
        db.commit()
        queue_notifications(
            db,
            event_type="quant_strategy_daily_performance",
            severity="info",
            subject=f"{config.name}每日绩效",
            payload={
                "strategy_config_id": config.id,
                "simulation_account_id": account.id,
                "trading_date": trading_date,
                "total_asset": total_asset,
                "daily_return": daily_return,
                "drawdown": drawdown,
            },
        )
        if breached:
            queue_notifications(
                db,
                event_type="quant_strategy_auto_paused",
                severity="critical",
                subject=f"{config.name}已触发风控暂停",
                payload={
                    "strategy_config_id": config.id,
                    "trading_date": trading_date,
                    "daily_return": daily_return,
                    "drawdown": drawdown,
                },
            )
        recorded += 1
    return {"recorded": recorded, "skipped": skipped, "paused": paused}
