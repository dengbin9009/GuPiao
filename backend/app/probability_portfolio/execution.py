from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Fill,
    Order,
    Position,
    ProbabilityCandidateDecision,
    ProbabilityPortfolioRun,
    Signal,
    SimulationAccount,
    SimulationAccountLedger,
    Stock,
    StrategyConfig,
    StrategyPositionLot,
    StrategyRun,
    StrategySchedule,
)
from ..simulation_accounts import daily_pnl_pct, revalue_account, snapshot_account
from .allocation import AllocationCandidate, allocate_portfolio, plan_buy_quantity
from .config import PROBABILITY_PORTFOLIO_DEFAULTS
from .readiness import (
    configuration_fingerprint,
    find_matching_dry_run,
    latest_qualified_artifact,
)


@dataclass(frozen=True)
class ScoredCandidate:
    stock_id: int
    symbol: str
    features: dict[str, float]
    raw_probability: float
    calibrated_probability: float
    expected_net_return: float
    volatility_20d: float


@dataclass(frozen=True)
class RejectedCandidate:
    stock_id: int
    symbol: str
    reasons: tuple[str, ...]
    features: dict[str, Any] | None = None


def _next_weekday(value):
    result = value + timedelta(days=1)
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result


def _existing_run(
    db: Session,
    config: StrategyConfig,
    *,
    trading_date: str,
    trigger_type: str,
) -> StrategyRun | None:
    portfolio_run = db.scalar(
        select(ProbabilityPortfolioRun).where(
            ProbabilityPortfolioRun.strategy_config_id == config.id,
            ProbabilityPortfolioRun.trading_date == trading_date,
            ProbabilityPortfolioRun.trigger_type == trigger_type,
        )
    )
    return db.get(StrategyRun, portfolio_run.strategy_run_id) if portfolio_run else None


def _complete_blocked(
    db: Session,
    run: StrategyRun,
    portfolio_run: ProbabilityPortfolioRun,
    reason: str,
) -> StrategyRun:
    run.status = "completed"
    run.finished_at = datetime.now(run.started_at.tzinfo)
    run.summary = {
        "accepted": 0,
        "selected": 0,
        "order_ids": [],
        "portfolio_run_id": portfolio_run.id,
        "reason": reason,
    }
    portfolio_run.status = "blocked"
    portfolio_run.error_message = reason
    portfolio_run.completed_at = run.finished_at
    db.commit()
    return run


def execute_portfolio_entry(
    db: Session,
    config: StrategyConfig,
    *,
    current: datetime,
    scored_candidates: list[ScoredCandidate],
    rejected_candidates: list[RejectedCandidate] | None = None,
    candidate_reasons: tuple[str, ...] = (),
    trigger_type: str = "portfolio_entry",
    dry_run: bool | None = None,
    summary_context: dict[str, Any] | None = None,
) -> StrategyRun:
    if config.mode != "SIMULATION" or not config.enabled or not config.simulation_account_id:
        raise ValueError("概率组合策略仅允许使用独立模拟账户")
    settings = get_settings()
    if settings.live_enabled or settings.broker_adapter != "simulation":
        raise ValueError("概率组合策略仅允许模拟盘运行")
    trading_date = current.date().isoformat()
    existing = _existing_run(
        db, config, trading_date=trading_date, trigger_type=trigger_type
    )
    if existing:
        return existing

    parameters = {**PROBABILITY_PORTFOLIO_DEFAULTS, **(config.parameters or {})}
    if dry_run is not None:
        parameters["dry_run"] = dry_run
    run = StrategyRun(
        strategy_config_id=config.id,
        mode="SIMULATION",
        status="running",
        started_at=current,
    )
    db.add(run)
    db.flush()
    portfolio_run = ProbabilityPortfolioRun(
        strategy_run_id=run.id,
        strategy_config_id=config.id,
        simulation_account_id=config.simulation_account_id,
        trading_date=trading_date,
        trigger_type=trigger_type,
        status="running",
        dry_run=bool(parameters["dry_run"]),
        config_fingerprint=configuration_fingerprint(
            parameters,
            simulation_account_id=config.simulation_account_id,
        ),
    )
    db.add(portfolio_run)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = _existing_run(
            db, config, trading_date=trading_date, trigger_type=trigger_type
        )
        if existing:
            return existing
        raise

    artifact = latest_qualified_artifact(db, parameters, current=current)
    if not parameters["dry_run"] and artifact is None:
        return _complete_blocked(db, run, portfolio_run, "概率模型尚未就绪")
    portfolio_run.model_artifact_id = artifact.id if artifact else None
    if (
        trigger_type == "portfolio_entry"
        and not parameters["dry_run"]
        and find_matching_dry_run(
            db,
            config,
            model_artifact_id=artifact.id,
        )
        is None
    ):
        return _complete_blocked(
            db,
            run,
            portfolio_run,
            "当前配置与模型尚未完成14:40专用无下单演练",
        )
    account = db.get(SimulationAccount, config.simulation_account_id)
    if not account or account.status != "active":
        return _complete_blocked(db, run, portfolio_run, "独立模拟账户不可用")
    if not parameters["dry_run"]:
        occupied = db.scalar(
            select(StrategyConfig.id).where(
                StrategyConfig.simulation_account_id == account.id,
                StrategyConfig.id != config.id,
            )
        )
        if occupied is not None:
            return _complete_blocked(db, run, portfolio_run, "独立模拟账户已被其他策略占用")
        enabled_triggers = set(
            db.scalars(
                select(StrategySchedule.trigger_type).where(
                StrategySchedule.strategy_config_id == config.id,
                StrategySchedule.enabled.is_(True),
            )
        )
        )
        if "portfolio_entry" not in enabled_triggers:
            return _complete_blocked(db, run, portfolio_run, "14:40入场计划未启用")
        if "portfolio_exit" not in enabled_triggers:
            return _complete_blocked(db, run, portfolio_run, "10:30退出计划未启用")
    revalue_account(db, account)
    daily_loss_limit = min(
        0.10,
        max(0.0, float(parameters["daily_loss_limit_pct"])),
    )
    if not parameters["dry_run"] and daily_pnl_pct(
        db, account, current=current
    ) <= -daily_loss_limit:
        return _complete_blocked(db, run, portfolio_run, "已触发概率组合日亏损熔断")
    if not parameters["dry_run"]:
        open_lot = db.scalar(
            select(StrategyPositionLot.id).where(
                StrategyPositionLot.strategy_config_id == config.id,
                StrategyPositionLot.account_id == account.id,
                StrategyPositionLot.status == "open",
                StrategyPositionLot.remaining_quantity > 0,
            )
        )
        if open_lot is not None:
            return _complete_blocked(db, run, portfolio_run, "存在尚未退出持仓，禁止叠加新仓")

    rejected_candidates = rejected_candidates or []
    snapshot_value = [
        {
            "stock_id": item.stock_id,
            "symbol": item.symbol,
            "features": item.features,
            "raw_probability": item.raw_probability,
            "calibrated_probability": item.calibrated_probability,
            "expected_net_return": item.expected_net_return,
            "volatility_20d": item.volatility_20d,
        }
        for item in sorted(scored_candidates, key=lambda row: row.symbol)
    ]
    snapshot_value.extend(
        {
            "stock_id": item.stock_id,
            "symbol": item.symbol,
            "features": item.features or {},
            "rejection_reasons": list(item.reasons),
        }
        for item in sorted(rejected_candidates, key=lambda row: row.symbol)
    )
    portfolio_run.snapshot_sha256 = hashlib.sha256(
        json.dumps(snapshot_value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    allocation = allocate_portfolio(
        [
            AllocationCandidate(
                stock_id=item.stock_id,
                symbol=item.symbol,
                probability=item.calibrated_probability,
                expected_net_return=item.expected_net_return,
                volatility_20d=item.volatility_20d,
            )
            for item in scored_candidates
        ],
        max_positions=min(10, max(1, int(parameters["max_positions"]))),
        min_probability=max(0.55, float(parameters["min_probability"])),
        min_expected_net_return=max(
            0.0,
            float(parameters["min_expected_net_return"]),
        ),
        min_position_pct=max(0.02, float(parameters["min_position_pct"])),
        max_position_pct=min(0.36, float(parameters["max_position_pct"])),
        min_total_exposure_pct=min(
            0.60,
            max(0.30, float(parameters["min_total_exposure_pct"])),
        ),
        max_total_exposure_pct=min(
            0.60,
            float(parameters["max_total_exposure_pct"]),
        ),
        volatility_floor=float(parameters["volatility_floor"]),
    )
    source_by_symbol = {item.symbol: item for item in scored_candidates}
    decisions: dict[str, ProbabilityCandidateDecision] = {}
    for rank, item in enumerate(allocation.allocations, start=1):
        source = source_by_symbol[item.symbol]
        decision = ProbabilityCandidateDecision(
            portfolio_run_id=portfolio_run.id,
            stock_id=item.stock_id,
            status="selected",
            rank=rank,
            features=source.features,
            rejection_reasons=[],
            raw_probability=source.raw_probability,
            calibrated_probability=item.probability,
            expected_net_return=item.expected_net_return,
            volatility_20d=item.volatility_20d,
            score=item.score,
            target_weight=item.target_weight,
            target_notional=account.total_asset * item.target_weight,
        )
        db.add(decision)
        decisions[item.symbol] = decision
    for item in allocation.rejected:
        source = source_by_symbol[item.symbol]
        db.add(
            ProbabilityCandidateDecision(
                portfolio_run_id=portfolio_run.id,
                stock_id=item.stock_id,
                status="rejected",
                features=source.features,
                rejection_reasons=list(item.reasons),
                raw_probability=source.raw_probability,
                calibrated_probability=source.calibrated_probability,
                expected_net_return=source.expected_net_return,
                volatility_20d=source.volatility_20d,
            )
        )
    scored_stock_ids = {item.stock_id for item in scored_candidates}
    for item in rejected_candidates:
        if item.stock_id in scored_stock_ids:
            continue
        db.add(
            ProbabilityCandidateDecision(
                portfolio_run_id=portfolio_run.id,
                stock_id=item.stock_id,
                status="rejected",
                features=item.features or {},
                rejection_reasons=list(item.reasons),
            )
        )
    db.flush()

    summary: dict[str, Any] = {
        "dry_run": bool(parameters["dry_run"]),
        "portfolio_run_id": portfolio_run.id,
        "scored_count": len(scored_candidates),
        "data_quality_rejected": len(rejected_candidates),
        "data_ready": bool(scored_candidates) and not candidate_reasons,
        "candidate_reasons": list(candidate_reasons),
        "selected": len(allocation.allocations),
        "target_total_weight": allocation.target_total_weight,
        "actual_total_weight": allocation.total_weight,
        "order_ids": [],
        "skipped": [],
        **(summary_context or {}),
    }
    if parameters["dry_run"]:
        run.status = "completed"
        run.finished_at = current
        run.summary = {**summary, "accepted": 0}
        portfolio_run.status = "completed"
        portfolio_run.error_message = (
            ", ".join(candidate_reasons) if candidate_reasons else None
        )
        portfolio_run.selected_count = len(allocation.allocations)
        portfolio_run.completed_at = current
        db.commit()
        return run

    order_ids: list[int] = []
    skipped: list[dict[str, str]] = []
    for item in allocation.allocations:
        stock = db.get(Stock, item.stock_id)
        planned = plan_buy_quantity(
            total_asset=float(account.total_asset),
            available_cash=float(account.available_cash),
            target_weight=item.target_weight,
            market_price=float(stock.last_price or 0) if stock else 0,
            slippage_bps=float(account.slippage_bps),
            commission_rate=float(account.commission_rate),
            min_commission=float(account.min_commission),
            transfer_fee_rate=float(account.transfer_fee_rate),
        )
        decision = decisions[item.symbol]
        decision.planned_quantity = planned.quantity
        if planned.quantity <= 0:
            decision.status = "skipped"
            decision.rejection_reasons = ["目标资金不足一手"]
            skipped.append({"symbol": item.symbol, "reason": "目标资金不足一手"})
            continue
        signal = Signal(
            strategy_run_id=run.id,
            stock_id=item.stock_id,
            side="buy",
            quantity=planned.quantity,
            price_type="market",
            reason=f"概率组合目标仓位 {item.target_weight:.2%}",
        )
        db.add(signal)
        db.flush()
        order = Order(
            account_id=account.id,
            mode="SIMULATION",
            strategy_run_id=run.id,
            signal_id=signal.id,
            stock_id=item.stock_id,
            side="buy",
            quantity=planned.quantity,
            price_type="market",
            status="filled",
            submitted_at=current,
        )
        db.add(order)
        db.flush()
        fill = Fill(
            order_id=order.id,
            account_id=account.id,
            stock_id=item.stock_id,
            mode="SIMULATION",
            quantity=planned.quantity,
            price=planned.fill_price,
            commission=planned.commission,
            transfer_fee=planned.transfer_fee,
            slippage_amount=(planned.fill_price - float(stock.last_price))
            * planned.quantity,
            filled_at=current,
        )
        db.add(fill)
        db.flush()
        position = db.scalar(
            select(Position).where(
                Position.account_id == account.id,
                Position.mode == "SIMULATION",
                Position.stock_id == item.stock_id,
            )
        )
        if position is None:
            position = Position(
                account_id=account.id,
                mode="SIMULATION",
                stock_id=item.stock_id,
                quantity=0,
                available_quantity=0,
                average_cost=0,
                market_value=0,
                unrealized_pnl=0,
            )
            db.add(position)
        old_cost = float(position.average_cost) * position.quantity
        position.quantity += planned.quantity
        position.average_cost = (old_cost + planned.total_cost) / position.quantity
        account.cash_balance -= planned.total_cost
        account.available_cash -= planned.total_cost
        db.add(
            SimulationAccountLedger(
                simulation_account_id=account.id,
                event_type="fill",
                amount=-planned.total_cost,
                balance_after=account.cash_balance,
                related_order_id=order.id,
                related_fill_id=fill.id,
                message=f"概率组合模拟买入 {item.symbol} {planned.quantity} 股",
            )
        )
        exit_date = _next_weekday(current.date())
        exit_time = time.fromisoformat(str(parameters["exit_time"]))
        db.add(
            StrategyPositionLot(
                strategy_config_id=config.id,
                account_id=account.id,
                stock_id=item.stock_id,
                buy_order_id=order.id,
                buy_fill_id=fill.id,
                original_quantity=planned.quantity,
                remaining_quantity=planned.quantity,
                available_on=exit_date.isoformat(),
                planned_exit_at=datetime.combine(exit_date, exit_time, tzinfo=current.tzinfo),
                status="open",
            )
        )
        decision.status = "filled"
        decision.order_id = order.id
        order_ids.append(order.id)

    snapshot_account(db, account, source="probability_portfolio_simulated_broker")
    run.status = "completed"
    run.finished_at = current
    run.summary = {
        **summary,
        "accepted": len(order_ids),
        "order_ids": order_ids,
        "skipped": skipped,
    }
    portfolio_run.status = "completed"
    portfolio_run.selected_count = len(allocation.allocations)
    portfolio_run.order_ids = order_ids
    portfolio_run.completed_at = current
    db.commit()
    return run


def execute_portfolio_exit(
    db: Session,
    config: StrategyConfig,
    *,
    current: datetime,
) -> StrategyRun:
    if config.mode != "SIMULATION" or not config.simulation_account_id:
        raise ValueError("概率组合退出仅允许模拟盘")
    settings = get_settings()
    if settings.live_enabled or settings.broker_adapter != "simulation":
        raise ValueError("概率组合退出仅允许模拟盘运行")
    trading_date = current.date().isoformat()
    existing_portfolio_run = db.scalar(
        select(ProbabilityPortfolioRun).where(
            ProbabilityPortfolioRun.strategy_config_id == config.id,
            ProbabilityPortfolioRun.trading_date == trading_date,
            ProbabilityPortfolioRun.trigger_type == "portfolio_exit",
        )
    )
    existing = (
        db.get(StrategyRun, existing_portfolio_run.strategy_run_id)
        if existing_portfolio_run
        else None
    )
    if existing and not bool((existing.summary or {}).get("retryable")):
        return existing
    parameters = {**PROBABILITY_PORTFOLIO_DEFAULTS, **(config.parameters or {})}
    if existing and existing_portfolio_run:
        run = existing
        run.status = "running"
        run.finished_at = None
        run.summary = {}
        existing_portfolio_run.status = "running"
        existing_portfolio_run.error_message = None
        existing_portfolio_run.completed_at = None
        portfolio_run = existing_portfolio_run
    else:
        run = StrategyRun(
            strategy_config_id=config.id,
            mode="SIMULATION",
            status="running",
            started_at=current,
        )
        db.add(run)
        db.flush()
        portfolio_run = ProbabilityPortfolioRun(
            strategy_run_id=run.id,
            strategy_config_id=config.id,
            simulation_account_id=config.simulation_account_id,
            trading_date=trading_date,
            trigger_type="portfolio_exit",
            status="running",
            dry_run=False,
        )
        db.add(portfolio_run)
        db.flush()
    account = db.get(SimulationAccount, config.simulation_account_id)
    if not account or account.status != "active":
        return _complete_blocked(db, run, portfolio_run, "独立模拟账户不可用")

    lots = list(
        db.scalars(
            select(StrategyPositionLot)
            .where(
                StrategyPositionLot.strategy_config_id == config.id,
                StrategyPositionLot.account_id == account.id,
                StrategyPositionLot.status == "open",
                StrategyPositionLot.remaining_quantity > 0,
                StrategyPositionLot.available_on <= trading_date,
            )
            .order_by(StrategyPositionLot.stock_id, StrategyPositionLot.id)
        )
    )
    if not lots:
        return _complete_blocked(db, run, portfolio_run, "没有可卖的概率组合持仓")
    latest_time = time.fromisoformat(str(parameters["latest_exit_time"]))
    if current.time().replace(tzinfo=None) > latest_time:
        run.status = "completed"
        run.finished_at = current
        run.summary = {
            "accepted": 0,
            "order_ids": [],
            "reason": "已超过10:45，持仓保留至下一交易日",
            "retryable": False,
        }
        portfolio_run.status = "blocked"
        portfolio_run.error_message = run.summary["reason"]
        portfolio_run.completed_at = current
        db.commit()
        return run

    stocks = {stock.id: stock for stock in db.scalars(
        select(Stock).where(Stock.id.in_({lot.stock_id for lot in lots}))
    )}
    stale: list[str] = []
    untradable: list[str] = []
    for lot in lots:
        stock = stocks.get(lot.stock_id)
        quote_at = stock.quote_updated_at if stock else None
        if quote_at is not None and quote_at.tzinfo is None:
            quote_at = quote_at.replace(tzinfo=current.tzinfo)
        if (
            not stock
            or not stock.last_price
            or quote_at is None
            or (current - quote_at).total_seconds() < 0
            or (current - quote_at).total_seconds() > 60
        ):
            stale.append(stock.symbol if stock else str(lot.stock_id))
        elif (
            stock.status != "active"
            or (
                stock.limit_down_price is not None
                and float(stock.last_price) <= float(stock.limit_down_price) + 1e-9
            )
        ):
            untradable.append(stock.symbol)
    if stale or untradable:
        reason_parts = []
        if stale:
            reason_parts.append(
                f"退出行情缺失或已过期: {', '.join(sorted(set(stale))[:5])}"
            )
        if untradable:
            reason_parts.append(
                f"退出股票当前不可交易: {', '.join(sorted(set(untradable))[:5])}"
            )
        run.status = "completed"
        run.finished_at = current
        run.summary = {
            "accepted": 0,
            "order_ids": [],
            "reason": "; ".join(reason_parts),
            "retryable": True,
        }
        portfolio_run.status = "blocked"
        portfolio_run.error_message = run.summary["reason"]
        portfolio_run.completed_at = current
        for lot in lots:
            lot.last_exit_attempt_at = current
        db.commit()
        return run

    order_ids: list[int] = []
    by_stock: dict[int, list[StrategyPositionLot]] = {}
    for lot in lots:
        by_stock.setdefault(lot.stock_id, []).append(lot)
    positions: dict[int, Position] = {}
    insufficient: list[str] = []
    for stock_id, stock_lots in by_stock.items():
        quantity = sum(lot.remaining_quantity for lot in stock_lots)
        position = db.scalar(
            select(Position).where(
                Position.account_id == account.id,
                Position.mode == "SIMULATION",
                Position.stock_id == stock_id,
            )
        )
        if position is None or position.quantity < quantity:
            insufficient.append(stocks[stock_id].symbol)
        else:
            positions[stock_id] = position
    if insufficient:
        run.status = "completed"
        run.finished_at = current
        run.summary = {
            "accepted": 0,
            "order_ids": [],
            "reason": (
                "退出可卖数量不足: "
                f"{', '.join(sorted(set(insufficient))[:5])}"
            ),
            "retryable": True,
        }
        portfolio_run.status = "blocked"
        portfolio_run.error_message = run.summary["reason"]
        portfolio_run.completed_at = current
        db.commit()
        return run
    for stock_id, stock_lots in sorted(by_stock.items()):
        stock = stocks[stock_id]
        quantity = sum(lot.remaining_quantity for lot in stock_lots)
        position = positions[stock_id]
        position.available_quantity = max(position.available_quantity, quantity)
        fill_price = float(stock.last_price) * (1 - float(account.slippage_bps) / 10_000)
        notional = fill_price * quantity
        commission = max(notional * float(account.commission_rate), float(account.min_commission))
        stamp_tax = notional * float(account.stamp_tax_rate)
        transfer_fee = notional * float(account.transfer_fee_rate)
        proceeds = notional - commission - stamp_tax - transfer_fee
        signal = Signal(
            strategy_run_id=run.id,
            stock_id=stock_id,
            side="sell",
            quantity=quantity,
            price_type="market",
            reason="概率组合下一交易日10:30退出",
        )
        db.add(signal)
        db.flush()
        order = Order(
            account_id=account.id,
            mode="SIMULATION",
            strategy_run_id=run.id,
            signal_id=signal.id,
            stock_id=stock_id,
            side="sell",
            quantity=quantity,
            price_type="market",
            status="filled",
            submitted_at=current,
        )
        db.add(order)
        db.flush()
        fill = Fill(
            order_id=order.id,
            account_id=account.id,
            stock_id=stock_id,
            mode="SIMULATION",
            quantity=quantity,
            price=fill_price,
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            slippage_amount=(float(stock.last_price) - fill_price) * quantity,
            filled_at=current,
        )
        db.add(fill)
        db.flush()
        account.realized_pnl += proceeds - float(position.average_cost) * quantity
        account.cash_balance += proceeds
        account.available_cash += proceeds
        position.quantity -= quantity
        position.available_quantity = max(0, position.available_quantity - quantity)
        if position.quantity == 0:
            position.average_cost = 0
            position.market_value = 0
            position.unrealized_pnl = 0
        db.add(
            SimulationAccountLedger(
                simulation_account_id=account.id,
                event_type="fill",
                amount=proceeds,
                balance_after=account.cash_balance,
                related_order_id=order.id,
                related_fill_id=fill.id,
                message=f"概率组合模拟卖出 {stock.symbol} {quantity} 股",
            )
        )
        for lot in stock_lots:
            lot.remaining_quantity = 0
            lot.status = "closed"
            lot.last_exit_attempt_at = current
            lot.close_order_ids = [*(lot.close_order_ids or []), order.id]
        order_ids.append(order.id)

    snapshot_account(db, account, source="probability_portfolio_simulated_broker")
    run.status = "completed"
    run.finished_at = current
    run.summary = {
        "accepted": len(order_ids),
        "order_ids": order_ids,
        "retryable": False,
    }
    portfolio_run.status = "completed"
    portfolio_run.order_ids = order_ids
    portfolio_run.completed_at = current
    db.commit()
    return run
