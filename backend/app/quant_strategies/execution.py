from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Fill,
    Order,
    Position,
    QuantPortfolioDecision,
    QuantCandidateScore,
    RiskEvent,
    RiskSettings,
    Signal,
    SimulationAccount,
    SimulationAccountLedger,
    Stock,
    StrategyConfig,
    StrategyDefinition,
    StrategyPerformanceDaily,
    StrategyPositionLot,
    StrategyRiskProfile,
    StrategyRun,
)
from ..simulation_accounts import daily_pnl_pct, revalue_account, snapshot_account
from ..notifications import queue_notifications
from .catalog import QUANT_STRATEGY_SPECS
from .readiness import configuration_fingerprint, corporate_event_data_reason


@dataclass(frozen=True)
class PlannedOrder:
    stock_id: int
    symbol: str
    side: str
    quantity: int
    market_price: float
    fill_price: float
    commission: float
    stamp_tax: float
    transfer_fee: float
    cash_delta: float
    target_weight: float


def _blocked(
    db: Session,
    run: StrategyRun,
    decision: QuantPortfolioDecision,
    reason: str,
) -> StrategyRun:
    run.status = "completed"
    run.finished_at = run.started_at
    run.summary = {
        "accepted": 0,
        "precheck_passed": False,
        "decision_id": decision.id,
        "order_ids": [],
        "reason": reason,
    }
    decision.status = "blocked"
    decision.error_message = reason
    decision.completed_at = run.finished_at
    db.add(
        RiskEvent(
            mode="SIMULATION",
            event_type="quant_strategy_rebalance_blocked",
            strategy_run_id=run.id,
            message=reason,
            context={
                "strategy_config_id": decision.strategy_config_id,
                "simulation_account_id": decision.simulation_account_id,
                "decision_id": decision.id,
            },
        )
    )
    db.commit()
    queue_notifications(
        db,
        event_type="quant_strategy_risk_block",
        severity="warning",
        subject="独立量化策略调仓被风控拦截",
        payload={
            "strategy_config_id": decision.strategy_config_id,
            "simulation_account_id": decision.simulation_account_id,
            "decision_id": decision.id,
            "reason": reason,
        },
    )
    return run


def _retryable_blocked(
    db: Session,
    run: StrategyRun,
    decision: QuantPortfolioDecision,
    reason: str,
) -> StrategyRun:
    run.status = "completed"
    run.finished_at = run.started_at
    run.summary = {
        "accepted": 0,
        "precheck_passed": False,
        "decision_id": decision.id,
        "order_ids": [],
        "reason": reason,
        "retryable": True,
    }
    decision.strategy_run_id = None
    decision.status = "ready"
    decision.error_message = reason
    decision.completed_at = None
    db.add(
        RiskEvent(
            mode="SIMULATION",
            event_type="quant_strategy_rebalance_retry",
            strategy_run_id=run.id,
            message=reason,
            context={
                "strategy_config_id": decision.strategy_config_id,
                "simulation_account_id": decision.simulation_account_id,
                "decision_id": decision.id,
            },
        )
    )
    db.commit()
    return run


def _quote_is_stale(stock: Stock, *, current: datetime, max_age_seconds: int = 60) -> bool:
    updated_at = stock.quote_updated_at
    if updated_at is None:
        return True
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=current.tzinfo)
    age = (current - updated_at).total_seconds()
    return age < 0 or age > max_age_seconds


def _available_to_sell(
    db: Session,
    position: Position,
    *,
    current: datetime,
) -> int:
    cutoff = datetime.combine(current.date(), datetime.min.time(), tzinfo=current.tzinfo)
    prior_buys = db.scalar(
        select(func.coalesce(func.sum(Fill.quantity), 0))
        .join(Order, Fill.order_id == Order.id)
        .where(
            Fill.account_id == position.account_id,
            Fill.mode == "SIMULATION",
            Fill.stock_id == position.stock_id,
            Order.side == "buy",
            Fill.filled_at < cutoff,
        )
    ) or 0
    prior_sells = db.scalar(
        select(func.coalesce(func.sum(Fill.quantity), 0))
        .join(Order, Fill.order_id == Order.id)
        .where(
            Fill.account_id == position.account_id,
            Fill.mode == "SIMULATION",
            Fill.stock_id == position.stock_id,
            Order.side == "sell",
            Fill.filled_at <= current,
        )
    ) or 0
    historical_available = max(int(prior_buys - prior_sells), 0)
    return min(position.quantity, max(position.available_quantity, historical_available))


def _quote_readiness_reason(
    db: Session,
    *,
    account: SimulationAccount,
    targets: dict[str, float],
    current: datetime,
) -> str | None:
    held_symbols = set(
        db.scalars(
            select(Stock.symbol)
            .join(Position, Position.stock_id == Stock.id)
            .where(
                Position.account_id == account.id,
                Position.mode == "SIMULATION",
                Position.quantity > 0,
            )
        )
    )
    symbols = set(targets) | held_symbols
    stocks = {
        stock.symbol: stock
        for stock in db.scalars(select(Stock).where(Stock.symbol.in_(symbols)))
    }
    for symbol in sorted(symbols):
        stock = stocks.get(symbol)
        if stock is None:
            continue
        if not stock.last_price:
            return f"{symbol} 行情缺失"
        if _quote_is_stale(stock, current=current):
            return f"{symbol} 行情已过期"
    return None


def _plan_orders(
    db: Session,
    *,
    config: StrategyConfig,
    definition: StrategyDefinition,
    account: SimulationAccount,
    risk: StrategyRiskProfile,
    targets: dict[str, float],
    current: datetime,
) -> list[PlannedOrder]:
    spec = QUANT_STRATEGY_SPECS[definition.key]
    if len(targets) > spec.max_positions:
        raise ValueError("目标持仓数量超过策略上限")
    if any(value < 0 or value > spec.max_position_pct + 1e-9 for value in targets.values()):
        raise ValueError("目标单证券仓位超过策略上限")
    if sum(targets.values()) > spec.max_total_exposure_pct + 1e-9:
        raise ValueError("目标总仓位超过策略上限")
    if risk.emergency_stop_enabled:
        raise ValueError("策略紧急停止已启用")
    system_risk = db.scalar(
        select(RiskSettings).where(RiskSettings.mode == "SIMULATION")
    )
    if system_risk and system_risk.emergency_stop_enabled:
        raise ValueError("系统级紧急停止已启用")
    if risk.consecutive_errors >= risk.max_consecutive_errors:
        raise ValueError("策略连续错误次数达到暂停阈值")
    if daily_pnl_pct(db, account, current=current) <= -abs(risk.daily_loss_limit_pct):
        raise ValueError("策略已触发日亏损熔断")
    peak = db.scalar(
        select(func.max(StrategyPerformanceDaily.total_asset)).where(
            StrategyPerformanceDaily.strategy_config_id == config.id
        )
    )
    if peak and peak > 0 and (
        float(account.total_asset) / float(peak) - 1
    ) <= -abs(risk.max_drawdown_pct):
        raise ValueError("策略已触发最大回撤熔断")

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
    if len(stocks) != len(symbols):
        raise ValueError("目标组合包含未知证券")
    total_asset = float(account.total_asset)
    if total_asset <= 0:
        raise ValueError("模拟账户总资产无效")
    plans: list[PlannedOrder] = []
    for symbol in symbols:
        stock = stocks[symbol]
        if stock.status != "active" or not stock.last_price:
            raise ValueError(f"{symbol} 当前不可交易或行情缺失")
        if _quote_is_stale(stock, current=current):
            raise ValueError(f"{symbol} 行情已过期")
        market_price = float(stock.last_price)
        lot_size = max(int(stock.lot_size), 1)
        slippage = float(account.slippage_bps) / 10_000
        sizing_price = market_price * (1 + slippage)
        target_quantity = math.floor(
            total_asset * targets.get(symbol, 0.0) / sizing_price / lot_size
        ) * lot_size
        current_quantity = positions[symbol].quantity if symbol in positions else 0
        difference = target_quantity - current_quantity
        if abs(difference) < lot_size:
            continue
        side = "buy" if difference > 0 else "sell"
        quantity = abs(difference)
        if side == "buy" and stock.limit_up_price is not None and market_price >= float(stock.limit_up_price) - 1e-9:
            raise ValueError(f"{symbol} 当前涨停不可买入")
        if side == "sell" and stock.limit_down_price is not None and market_price <= float(stock.limit_down_price) + 1e-9:
            raise ValueError(f"{symbol} 当前跌停不可卖出")
        if side == "sell":
            available_to_sell = _available_to_sell(
                db,
                positions[symbol],
                current=current,
            )
            positions[symbol].available_quantity = available_to_sell
            if quantity > available_to_sell:
                raise ValueError(f"{symbol} 可卖数量不足，受 A 股 T+1 限制")
        if (
            side == "buy"
            and quantity * market_price
            > total_asset * risk.max_order_notional_pct + 1e-6
        ):
            raise ValueError(f"{symbol} 单笔订单金额超过策略风控上限")
        fill_price = market_price * (1 + slippage if side == "buy" else 1 - slippage)
        notional = fill_price * quantity
        commission = max(notional * float(account.commission_rate), float(account.min_commission))
        stamp_tax = notional * float(account.stamp_tax_rate) if side == "sell" else 0.0
        transfer_fee = notional * float(account.transfer_fee_rate)
        cash_delta = (
            -(notional + commission + transfer_fee)
            if side == "buy"
            else notional - commission - stamp_tax - transfer_fee
        )
        plans.append(
            PlannedOrder(
                stock.id,
                symbol,
                side,
                quantity,
                market_price,
                fill_price,
                commission,
                stamp_tax,
                transfer_fee,
                cash_delta,
                targets.get(symbol, 0.0),
            )
        )
    plans.sort(key=lambda row: (row.side != "sell", row.symbol))
    if len(plans) > risk.max_daily_orders:
        raise ValueError("目标组合订单数超过策略风控上限")
    projected_cash = float(account.available_cash) + sum(plan.cash_delta for plan in plans)
    if projected_cash < -1e-6:
        raise ValueError("完整目标组合的可用资金不足")
    return plans


def _record_performance(
    db: Session,
    config: StrategyConfig,
    account: SimulationAccount,
    *,
    current: datetime,
) -> StrategyPerformanceDaily:
    valuation = revalue_account(db, account)
    previous = db.scalar(
        select(StrategyPerformanceDaily)
        .where(
            StrategyPerformanceDaily.strategy_config_id == config.id,
            StrategyPerformanceDaily.trading_date < current.date().isoformat(),
        )
        .order_by(StrategyPerformanceDaily.trading_date.desc())
        .limit(1)
    )
    peak = db.scalar(
        select(func.max(StrategyPerformanceDaily.total_asset)).where(
            StrategyPerformanceDaily.strategy_config_id == config.id
        )
    ) or valuation.total_asset
    peak = max(float(peak), float(valuation.total_asset))
    opening = float(previous.total_asset) if previous and previous.total_asset else account.initial_cash
    row = db.scalar(
        select(StrategyPerformanceDaily).where(
            StrategyPerformanceDaily.strategy_config_id == config.id,
            StrategyPerformanceDaily.trading_date == current.date().isoformat(),
        )
    )
    if row is None:
        row = StrategyPerformanceDaily(
            strategy_config_id=config.id,
            simulation_account_id=account.id,
            trading_date=current.date().isoformat(),
            cash_balance=account.cash_balance,
            market_value=valuation.market_value,
            total_asset=valuation.total_asset,
        )
        db.add(row)
    row.cash_balance = account.cash_balance
    row.market_value = valuation.market_value
    row.total_asset = valuation.total_asset
    row.daily_return = valuation.total_asset / opening - 1 if opening else 0
    row.cumulative_return = valuation.total_asset / account.initial_cash - 1
    row.drawdown = valuation.total_asset / peak - 1 if peak else 0
    row.exposure = valuation.market_value / valuation.total_asset if valuation.total_asset else 0
    row.captured_at = current
    return row


def execute_quant_rebalance(
    db: Session,
    decision: QuantPortfolioDecision,
    *,
    current: datetime,
    dry_run: bool,
    next_sellable_date: str | None = None,
) -> StrategyRun:
    existing = (
        db.get(StrategyRun, decision.strategy_run_id)
        if decision.strategy_run_id
        else None
    )
    if existing:
        return existing
    config = db.get(StrategyConfig, decision.strategy_config_id)
    definition = db.get(StrategyDefinition, config.strategy_definition_id) if config else None
    run = StrategyRun(
        strategy_config_id=config.id if config else decision.strategy_config_id,
        mode="SIMULATION",
        status="running",
        started_at=current,
    )
    db.add(run)
    db.flush()
    decision.strategy_run_id = run.id
    settings = get_settings()
    if (
        config is None
        or definition is None
        or definition.key not in QUANT_STRATEGY_SPECS
        or config.mode != "SIMULATION"
        or config.simulation_account_id != decision.simulation_account_id
        or settings.live_enabled
        or settings.broker_adapter != "simulation"
    ):
        return _blocked(db, run, decision, "独立量化策略仅允许使用绑定的模拟账户")
    expected_fingerprint = configuration_fingerprint(
        config.parameters or {},
        simulation_account_id=config.simulation_account_id,
        strategy_version=definition.version,
    )
    if decision.config_fingerprint != expected_fingerprint:
        return _blocked(db, run, decision, "策略配置在信号生成后发生变化")
    account = db.get(SimulationAccount, config.simulation_account_id)
    risk = db.scalar(
        select(StrategyRiskProfile).where(
            StrategyRiskProfile.strategy_config_id == config.id
        )
    )
    if account is None or account.status != "active" or risk is None:
        return _blocked(db, run, decision, "独立模拟账户或策略风控不可用")
    if next_sellable_date is None:
        candidate = current.date() + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        next_sellable_date = candidate.isoformat()
    try:
        parsed_sellable_date = datetime.fromisoformat(next_sellable_date).date()
    except ValueError:
        return _blocked(db, run, decision, "下一可卖交易日格式无效")
    if parsed_sellable_date <= current.date():
        return _blocked(db, run, decision, "下一可卖交易日必须晚于买入日")
    if "events" in QUANT_STRATEGY_SPECS[definition.key].required_datasets:
        event_reason = corporate_event_data_reason(db, current=current)
        if event_reason:
            return _retryable_blocked(db, run, decision, event_reason)
    targets = {
        str(key): float(value)
        for key, value in decision.target_weights.items()
    }
    quote_reason = _quote_readiness_reason(
        db,
        account=account,
        targets=targets,
        current=current,
    )
    if quote_reason:
        return _retryable_blocked(db, run, decision, quote_reason)
    revalue_account(db, account)
    try:
        plans = _plan_orders(
            db,
            config=config,
            definition=definition,
            account=account,
            risk=risk,
            targets=targets,
            current=current,
        )
    except ValueError as exc:
        reason = str(exc)
        if "行情已过期" in reason or "行情缺失" in reason:
            return _retryable_blocked(db, run, decision, reason)
        return _blocked(db, run, decision, reason)

    summary = {
        "accepted": len(plans),
        "precheck_passed": True,
        "dry_run": dry_run,
        "decision_id": decision.id,
        "target_weights": decision.target_weights,
        "planned_orders": [
            {"symbol": plan.symbol, "side": plan.side, "quantity": plan.quantity}
            for plan in plans
        ],
        "order_ids": [],
    }
    if dry_run:
        run.status = "completed"
        run.finished_at = current
        run.summary = summary
        decision.status = "dry_run_completed"
        decision.order_ids = []
        decision.completed_at = current
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
            reason=f"{definition.name}目标仓位 {plan.target_weight:.2%}",
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
            remaining = plan.quantity
            lots = list(
                db.scalars(
                    select(StrategyPositionLot)
                    .where(
                        StrategyPositionLot.strategy_config_id == config.id,
                        StrategyPositionLot.account_id == account.id,
                        StrategyPositionLot.stock_id == plan.stock_id,
                        StrategyPositionLot.status == "open",
                        StrategyPositionLot.remaining_quantity > 0,
                    )
                    .order_by(StrategyPositionLot.id)
                )
            )
            for lot in lots:
                if remaining <= 0:
                    break
                closed = min(lot.remaining_quantity, remaining)
                lot.remaining_quantity -= closed
                lot.close_order_ids = [*(lot.close_order_ids or []), order.id]
                if lot.remaining_quantity == 0:
                    lot.status = "closed"
                remaining -= closed
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
            old_cost = float(position.average_cost) * position.quantity
            position.quantity += plan.quantity
            position.average_cost = (old_cost - plan.cash_delta) / position.quantity
            candidate_score = db.scalar(
                select(QuantCandidateScore).where(
                    QuantCandidateScore.decision_id == decision.id,
                    QuantCandidateScore.stock_id == plan.stock_id,
                )
            )
            candidate_features = (
                dict(candidate_score.features or {}) if candidate_score else {}
            )
            db.add(
                StrategyPositionLot(
                    strategy_config_id=config.id,
                    account_id=account.id,
                    stock_id=plan.stock_id,
                    buy_order_id=order.id,
                    buy_fill_id=fill.id,
                    original_quantity=plan.quantity,
                    remaining_quantity=plan.quantity,
                    available_on=parsed_sellable_date.isoformat(),
                    planned_exit_at=current + timedelta(days=3650),
                    status="open",
                    strategy_metadata={
                        "strategy_key": definition.key,
                        "decision_id": decision.id,
                        "entry_date": current.date().isoformat(),
                        "entry_atr": candidate_features.get("atr_20d"),
                        "report_period": candidate_features.get("report_period"),
                    },
                )
            )
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
                message=f"{definition.name}模拟{('买入' if plan.side == 'buy' else '卖出')} {plan.symbol} {plan.quantity} 股",
            )
        )
        order_ids.append(order.id)

    snapshot_account(db, account, source="quant_strategy_simulated_broker")
    _record_performance(db, config, account, current=current)
    run.status = "completed"
    run.finished_at = current
    run.summary = {**summary, "order_ids": order_ids}
    decision.status = "executed"
    decision.order_ids = order_ids
    decision.completed_at = current
    risk.consecutive_errors = 0
    db.add(
        RiskEvent(
            mode="SIMULATION",
            event_type="quant_strategy_rebalance_success",
            strategy_run_id=run.id,
            message=f"{definition.name}模拟组合调仓完成",
            context={
                "strategy_config_id": config.id,
                "simulation_account_id": account.id,
                "decision_id": decision.id,
                "order_ids": order_ids,
            },
        )
    )
    db.commit()
    queue_notifications(
        db,
        event_type="quant_strategy_trade",
        severity="info",
        subject=f"{definition.name}模拟调仓完成",
        payload={
            "strategy_config_id": config.id,
            "simulation_account_id": account.id,
            "decision_id": decision.id,
            "order_ids": order_ids,
        },
    )
    return run
