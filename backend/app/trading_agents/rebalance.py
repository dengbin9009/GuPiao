from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..market_cache import quote_is_stale
from ..models import (
    AccountSnapshot,
    Fill,
    Order,
    Position,
    RiskEvent,
    RiskSettings,
    Signal,
    SimulationAccount,
    SimulationAccountLedger,
    Stock,
    StrategyConfig,
    StrategyRun,
    TradingAgentBatch,
    TradingAgentPortfolioDecision,
    now,
)
from .config import TRADING_AGENTS_DEFAULTS
from .config import configuration_fingerprint
from .runtime import simulation_account_is_available


@dataclass(frozen=True)
class PlannedOrder:
    stock_id: int
    symbol: str
    side: str
    quantity: int
    market_price: float
    fill_price: float
    notional: float
    commission: float
    stamp_tax: float
    transfer_fee: float
    cash_delta: float
    target_weight: float


def revalue_simulation_account(db: Session, account: SimulationAccount) -> None:
    positions = list(
        db.scalars(
            select(Position).where(
                Position.account_id == account.id,
                Position.mode == "SIMULATION",
                Position.quantity > 0,
            )
        )
    )
    market_value = 0.0
    unrealized = 0.0
    for position in positions:
        stock = db.get(Stock, position.stock_id)
        price = float(stock.last_price or 0)
        position.market_value = position.quantity * price
        position.unrealized_pnl = (
            price - float(position.average_cost)
        ) * position.quantity
        market_value += position.market_value
        unrealized += position.unrealized_pnl
    account.total_asset = float(account.cash_balance) + market_value
    account.unrealized_pnl = unrealized


def _latest_rebalance(current: datetime, value: str) -> datetime:
    parsed = time.fromisoformat(value)
    return datetime.combine(current.date(), parsed, tzinfo=current.tzinfo)


def _existing_run(db: Session, batch: TradingAgentBatch) -> StrategyRun | None:
    if batch.rebalance_run_id:
        return db.get(StrategyRun, batch.rebalance_run_id)
    return db.scalar(
        select(StrategyRun)
        .where(StrategyRun.strategy_config_id == batch.strategy_config_id)
        .order_by(StrategyRun.id.desc())
        .limit(1)
    ) if batch.status in {"rebalanced", "dry_run_completed"} else None


def _blocked(
    db: Session,
    batch: TradingAgentBatch,
    run: StrategyRun,
    message: str,
) -> StrategyRun:
    run.status = "completed"
    run.finished_at = now()
    run.summary = {
        "accepted": 0,
        "batch_id": batch.id,
        "reason": message,
        "order_ids": [],
    }
    batch.status = "blocked"
    batch.error_message = message
    batch.rebalance_run_id = run.id
    batch.order_ids = []
    db.add(
        RiskEvent(
            mode="SIMULATION",
            event_type="trading_agents_rebalance_blocked",
            strategy_run_id=run.id,
            message=message,
            context={
                "batch_id": batch.id,
                "simulation_account_id": batch.simulation_account_id,
            },
        )
    )
    db.commit()
    return run


def _available_to_sell(
    db: Session,
    position: Position,
    *,
    account_id: int,
    current: datetime,
) -> int:
    cutoff = datetime.combine(current.date(), datetime.min.time(), tzinfo=current.tzinfo)
    historical_buys = db.scalar(
        select(func.coalesce(func.sum(Fill.quantity), 0))
        .join(Order, Fill.order_id == Order.id)
        .where(
            Fill.account_id == account_id,
            Fill.mode == "SIMULATION",
            Fill.stock_id == position.stock_id,
            Order.side == "buy",
            Fill.filled_at < cutoff,
        )
    ) or 0
    historical_sells = db.scalar(
        select(func.coalesce(func.sum(Fill.quantity), 0))
        .join(Order, Fill.order_id == Order.id)
        .where(
            Fill.account_id == account_id,
            Fill.mode == "SIMULATION",
            Fill.stock_id == position.stock_id,
            Order.side == "sell",
            Fill.filled_at <= current,
        )
    ) or 0
    return min(
        position.quantity,
        max(int(position.available_quantity), int(historical_buys - historical_sells), 0),
    )


def _plan_orders(
    db: Session,
    *,
    batch: TradingAgentBatch,
    account: SimulationAccount,
    decision: TradingAgentPortfolioDecision,
    risk: RiskSettings,
    parameters: dict[str, Any],
    current: datetime,
) -> tuple[list[PlannedOrder], list[dict[str, Any]]]:
    targets = {str(key): float(value) for key, value in decision.target_weights.items()}
    if len(targets) > int(parameters["max_positions"]):
        raise ValueError("目标持仓数量超过上限")
    position_cap = min(
        float(parameters["max_position_pct"]),
        float(risk.max_position_pct),
    )
    exposure_cap = min(
        float(parameters["max_total_exposure_pct"]),
        float(risk.max_total_exposure_pct),
    )
    if any(value < 0 or value > position_cap for value in targets.values()):
        raise ValueError("目标单股仓位超过上限")
    if sum(targets.values()) > exposure_cap + 1e-9:
        raise ValueError("目标总仓位超过上限")
    if risk.emergency_stop_enabled:
        raise ValueError("已触发紧急停止")
    day_start = datetime.combine(current.date(), datetime.min.time(), tzinfo=current.tzinfo)
    opening_snapshot = db.scalar(
        select(AccountSnapshot)
        .where(
            AccountSnapshot.mode == "SIMULATION",
            AccountSnapshot.account_id == account.id,
            AccountSnapshot.captured_at >= day_start,
            AccountSnapshot.captured_at <= current,
        )
        .order_by(AccountSnapshot.captured_at, AccountSnapshot.id)
        .limit(1)
    )
    opening_asset = (
        float(opening_snapshot.total_asset)
        if opening_snapshot and opening_snapshot.total_asset > 0
        else float(account.initial_cash)
    )
    daily_pnl_pct = (
        (float(account.total_asset) - opening_asset) / opening_asset
        if opening_asset > 0
        else 0
    )
    if daily_pnl_pct <= -abs(float(risk.daily_loss_limit_pct)):
        raise ValueError("已触发日亏损熔断")
    recent_events = list(
        db.scalars(
            select(RiskEvent)
            .where(
                RiskEvent.mode == "SIMULATION",
                RiskEvent.created_at >= current - timedelta(days=30),
            )
            .order_by(RiskEvent.id.desc())
            .limit(max(20, int(risk.max_consecutive_errors) * 4))
        )
    )
    consecutive_errors = 0
    for event in recent_events:
        if int((event.context or {}).get("simulation_account_id", -1)) != account.id:
            continue
        if event.event_type == "trading_agents_rebalance_success":
            break
        if event.event_type in {
            "trading_agents_batch_failure",
            "trading_agents_rebalance_blocked",
        }:
            consecutive_errors += 1
        if consecutive_errors >= int(risk.max_consecutive_errors):
            raise ValueError("连续错误次数达到暂停阈值")

    positions = {
        stock.symbol: position
        for position, stock in db.execute(
            select(Position, Stock)
            .join(Stock, Stock.id == Position.stock_id)
            .where(
                Position.account_id == account.id,
                Position.mode == "SIMULATION",
                Position.quantity > 0,
            )
        )
    }
    symbols = sorted(set(targets) | set(positions))
    stocks = {
        stock.symbol: stock
        for stock in db.scalars(select(Stock).where(Stock.symbol.in_(symbols)))
    }
    total_asset = float(account.total_asset)
    if total_asset <= 0:
        raise ValueError("账户总资产无效")
    plans: list[PlannedOrder] = []
    skipped_orders: list[dict[str, Any]] = []
    for symbol in symbols:
        stock = stocks.get(symbol)
        if stock is None or not stock.last_price:
            raise ValueError(f"{symbol} 行情缺失")
        if quote_is_stale(
            stock.quote_updated_at,
            current=current,
            stale_after_seconds=int(parameters["snapshot_quote_max_age_seconds"]),
        ):
            raise ValueError(f"{symbol} 行情已过期")
        market_price = float(stock.last_price)
        target_quantity = math.floor(
            total_asset * targets.get(symbol, 0) / market_price / 100
        ) * 100
        current_quantity = positions[symbol].quantity if symbol in positions else 0
        difference = target_quantity - current_quantity
        if abs(difference) < 100:
            skipped_orders.append(
                {
                    "symbol": symbol,
                    "reason": "目标差额不足一手",
                    "target_weight": targets.get(symbol, 0),
                }
            )
            continue
        side = "buy" if difference > 0 else "sell"
        quantity = abs(difference)
        if side == "sell":
            available = _available_to_sell(
                db,
                positions[symbol],
                account_id=account.id,
                current=current,
            )
            if quantity > available:
                raise ValueError(f"{symbol} 可卖数量不足，受 A 股 T+1 限制")
        market_notional = quantity * market_price
        max_order = min(
            float(risk.max_order_notional_abs),
            total_asset * float(risk.max_order_notional_pct),
        )
        if market_notional > max_order + 1e-6:
            raise ValueError(f"{symbol} 单笔订单金额超过风控上限")
        slip = float(account.slippage_bps) / 10_000
        fill_price = market_price * (1 + slip if side == "buy" else 1 - slip)
        notional = fill_price * quantity
        commission = max(notional * float(account.commission_rate), float(account.min_commission))
        stamp_tax = notional * float(account.stamp_tax_rate) if side == "sell" else 0
        transfer_fee = notional * float(account.transfer_fee_rate)
        cash_delta = (
            -(notional + commission + transfer_fee)
            if side == "buy"
            else notional - commission - stamp_tax - transfer_fee
        )
        plans.append(
            PlannedOrder(
                stock_id=stock.id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                market_price=market_price,
                fill_price=fill_price,
                notional=notional,
                commission=commission,
                stamp_tax=stamp_tax,
                transfer_fee=transfer_fee,
                cash_delta=cash_delta,
                target_weight=targets.get(symbol, 0),
            )
        )
    plans.sort(key=lambda item: (item.side != "sell", item.symbol))
    projected_cash = float(account.available_cash) + sum(item.cash_delta for item in plans)
    if projected_cash < -1e-6:
        raise ValueError("完整目标组合的可用资金不足")
    return plans, skipped_orders


def _record_snapshot(db: Session, account: SimulationAccount) -> None:
    market_value = float(account.total_asset) - float(account.cash_balance)
    db.add(
        AccountSnapshot(
            mode="SIMULATION",
            account_id=account.id,
            cash_balance=account.cash_balance,
            available_cash=account.available_cash,
            frozen_cash=account.frozen_cash,
            market_value=market_value,
            total_asset=account.total_asset,
            realized_pnl=account.realized_pnl,
            unrealized_pnl=account.unrealized_pnl,
            exposure=market_value / account.total_asset if account.total_asset else 0,
            source="trading_agents_simulated_broker",
        )
    )


def rebalance_batch(
    db: Session,
    batch: TradingAgentBatch,
    *,
    current: datetime | None = None,
    allow_outside_window: bool = False,
) -> StrategyRun:
    current = current or now()
    existing = _existing_run(db, batch)
    if existing:
        return existing
    config = db.get(StrategyConfig, batch.strategy_config_id)
    parameters = {**TRADING_AGENTS_DEFAULTS, **(config.parameters or {})}
    run = StrategyRun(
        strategy_config_id=config.id,
        mode="SIMULATION",
        status="running",
    )
    db.add(run)
    db.flush()
    expected_fingerprint = configuration_fingerprint(
        parameters,
        simulation_account_id=config.simulation_account_id,
    )
    if not batch.config_fingerprint or batch.config_fingerprint != expected_fingerprint:
        return _blocked(db, batch, run, "策略配置或模拟账户在分析后发生变化")
    settings = get_settings()
    if (
        config.mode != "SIMULATION"
        or not config.enabled
        or not simulation_account_is_available(
            db,
            account_id=config.simulation_account_id,
            strategy_config_id=config.id,
        )
        or settings.live_enabled
        or settings.broker_adapter != "simulation"
    ):
        return _blocked(db, batch, run, "TradingAgents 仅允许使用独立模拟账户运行")
    if batch.status != "ready":
        return _blocked(db, batch, run, "批次尚未完成全部分析")
    rebalance_after = batch.rebalance_after
    if rebalance_after.tzinfo is None:
        rebalance_after = rebalance_after.replace(tzinfo=current.tzinfo)
    if current < rebalance_after and not allow_outside_window:
        return _blocked(db, batch, run, "尚未到调仓时间")
    if (
        current > _latest_rebalance(current, str(parameters["latest_rebalance_time"]))
        and not allow_outside_window
    ):
        return _blocked(db, batch, run, "已错过最晚调仓时间")
    if allow_outside_window and not parameters["dry_run"]:
        return _blocked(db, batch, run, "只有无下单演练允许绕过调仓窗口")
    account = db.get(SimulationAccount, batch.simulation_account_id)
    risk = db.scalar(select(RiskSettings).where(RiskSettings.mode == "SIMULATION"))
    decision = db.scalar(
        select(TradingAgentPortfolioDecision).where(
            TradingAgentPortfolioDecision.batch_id == batch.id,
            TradingAgentPortfolioDecision.status == "ready",
        )
    )
    if not account or account.status != "active" or not risk or not decision:
        return _blocked(db, batch, run, "模拟账户、风控配置或组合决策不可用")
    revalue_simulation_account(db, account)
    try:
        plans, skipped_orders = _plan_orders(
            db,
            batch=batch,
            account=account,
            decision=decision,
            risk=risk,
            parameters=parameters,
            current=current,
        )
    except ValueError as exc:
        return _blocked(db, batch, run, str(exc))

    summary = {
        "accepted": len(plans),
        "batch_id": batch.id,
        "dry_run": bool(parameters["dry_run"]),
        "target_weights": decision.target_weights,
        "skipped_orders": skipped_orders,
        "planned_orders": [
            {
                "symbol": item.symbol,
                "side": item.side,
                "quantity": item.quantity,
            }
            for item in plans
        ],
    }
    if parameters["dry_run"]:
        run.status = "completed"
        run.finished_at = current
        run.summary = {**summary, "order_ids": []}
        batch.status = "dry_run_completed"
        batch.rebalance_run_id = run.id
        batch.order_ids = []
        db.commit()
        return run

    order_ids: list[int] = []
    for plan in plans:
        position = db.scalar(
            select(Position).where(
                Position.account_id == account.id,
                Position.mode == "SIMULATION",
                Position.stock_id == plan.stock_id,
            )
        )
        signal = Signal(
            strategy_run_id=run.id,
            stock_id=plan.stock_id,
            side=plan.side,
            quantity=plan.quantity,
            price_type="market",
            reason=f"TradingAgents 目标仓位 {plan.target_weight:.2%}",
        )
        db.add(signal)
        db.flush()
        order = Order(
            account_id=account.id,
            mode="SIMULATION",
            strategy_run_id=run.id,
            signal_id=signal.id,
            stock_id=plan.stock_id,
            side=plan.side,
            quantity=plan.quantity,
            price_type="market",
            status="filled",
            submitted_at=current,
        )
        db.add(order)
        db.flush()
        fill = Fill(
            order_id=order.id,
            account_id=account.id,
            stock_id=plan.stock_id,
            mode="SIMULATION",
            quantity=plan.quantity,
            price=plan.fill_price,
            commission=plan.commission,
            stamp_tax=plan.stamp_tax,
            transfer_fee=plan.transfer_fee,
            slippage_amount=abs(plan.fill_price - plan.market_price) * plan.quantity,
            filled_at=current,
        )
        db.add(fill)
        db.flush()
        if plan.side == "sell":
            account.realized_pnl += (
                (plan.fill_price - float(position.average_cost)) * plan.quantity
                - plan.commission
                - plan.stamp_tax
                - plan.transfer_fee
            )
            position.quantity -= plan.quantity
            position.available_quantity = max(0, position.available_quantity - plan.quantity)
            if position.quantity == 0:
                position.average_cost = 0
        else:
            if position is None:
                position = Position(
                    account_id=account.id,
                    mode="SIMULATION",
                    stock_id=plan.stock_id,
                    quantity=0,
                    available_quantity=0,
                    average_cost=0,
                    market_value=0,
                    unrealized_pnl=0,
                )
                db.add(position)
            old_cost = position.average_cost * position.quantity
            position.quantity += plan.quantity
            position.average_cost = (
                old_cost - plan.cash_delta
            ) / position.quantity
        account.cash_balance += plan.cash_delta
        account.available_cash += plan.cash_delta
        db.add(
            SimulationAccountLedger(
                simulation_account_id=account.id,
                event_type="fill",
                amount=plan.cash_delta,
                balance_after=account.cash_balance,
                related_order_id=order.id,
                related_fill_id=fill.id,
                message=f"TradingAgents 模拟{('买入' if plan.side == 'buy' else '卖出')} {plan.symbol} {plan.quantity} 股",
            )
        )
        order_ids.append(order.id)

    revalue_simulation_account(db, account)
    _record_snapshot(db, account)
    run.status = "completed"
    run.finished_at = current
    run.summary = {**summary, "order_ids": order_ids}
    batch.status = "rebalanced"
    batch.rebalance_run_id = run.id
    batch.order_ids = order_ids
    db.add(
        RiskEvent(
            mode="SIMULATION",
            event_type="trading_agents_rebalance_success",
            strategy_run_id=run.id,
            message="TradingAgents 模拟组合调仓完成",
            context={
                "batch_id": batch.id,
                "simulation_account_id": batch.simulation_account_id,
                "order_ids": order_ids,
            },
        )
    )
    db.commit()
    return run
