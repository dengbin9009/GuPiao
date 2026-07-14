from __future__ import annotations

import ast
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import Session

from .brokers import build_broker_adapter
from .backtest_engine import BacktestEngine, BacktestRequest
from .config import Settings, get_settings, live_runtime_is_open
from .overnight_strategy import build_universe_candidates, select_best_candidate
from .market_data import StaleDataError, ensure_fresh
from .market_cache import quote_is_stale
from .models import (
    AccountSnapshot,
    Administrator,
    BacktestRun,
    BacktestTrade,
    BrokerGateway,
    DataSourceState,
    Fill,
    GatewayEvent,
    LiveTradingAccount,
    Order,
    Position,
    RiskEvent,
    RiskSettings,
    Signal,
    SimulationAccount,
    SimulationAccountLedger,
    Stock,
    StockEvent,
    StrategyConfig,
    StrategyDefinition,
    StrategyLog,
    StrategyRun,
    WatchlistItem,
    now,
)
from .risk import evaluate_order
from .notifications import queue_notifications
from .security import hash_password
from .trading_agents import TRADING_AGENTS_DEFAULTS


OVERNIGHT_DEFAULTS: dict[str, Any] = {
    "timezone": "Asia/Shanghai",
    "required_timeframe": "1m",
    "include_bse": False,
    "exclude_st": True,
    "min_listing_days": 60,
    "evaluation_time": "14:40",
    "entry_start": "14:45",
    "entry_end": "14:55",
    "min_turnover_amount": 100_000_000,
    "min_turnover_rate": 0.01,
    "min_intraday_return": 0.01,
    "max_intraday_return": 0.05,
    "benchmark_symbol": "000300.SH",
    "max_candidates": 3,
    "target_position_pct": 0.20,
    "exit_evaluation_time": "09:35",
    "force_exit_time": "09:45",
    "latest_exit_time": "10:00",
    "take_gap_pct": 0.015,
    "stop_gap_pct": -0.02,
    "event_risk_enabled": True,
}


def validate_strategy_parameters(schema: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties", {})
    unknown = sorted(set(parameters) - set(properties))
    if unknown:
        raise ValueError(f"未知策略参数: {', '.join(unknown)}")
    validated: dict[str, Any] = {}
    for name, definition in properties.items():
        default = definition.get("default")
        value = parameters.get(name, default)
        expected = type(default)
        valid_type = (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            if expected is float
            else type(value) is expected
        )
        if not valid_type:
            raise ValueError(f"参数 {name} 类型无效")
        if name in {"evaluation_time", "entry_start", "entry_end", "exit_evaluation_time", "force_exit_time", "latest_exit_time"}:
            try:
                time.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"参数 {name} 时间格式无效") from exc
        validated[name] = value
    if not 0 < float(validated.get("target_position_pct", 0)) <= 1:
        raise ValueError("参数 target_position_pct 必须在 0 到 1 之间")
    if int(validated.get("max_candidates", 0)) < 1:
        raise ValueError("参数 max_candidates 必须大于 0")
    return validated


def serialize(model: Any, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in inspect(model).mapper.column_attrs:
        value = getattr(model, column.key)
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        result[column.key] = value
    result.update(extra)
    return result


def seed_database(db: Session, settings: Settings) -> None:
    if not db.scalar(select(Administrator).where(Administrator.username == settings.admin_username)):
        db.add(
            Administrator(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
            )
        )

    if not db.scalar(select(Stock.id).limit(1)):
        stocks = [
            ("000001", "SZSE", "000001.SZ", "平安银行", "pinganyinhang", "payh", 12.42, 1.12),
            ("000858", "SZSE", "000858.SZ", "五粮液", "wuliangye", "wly", 128.30, 2.08),
            ("300750", "SZSE", "300750.SZ", "宁德时代", "ningdeshidai", "ndsd", 192.56, 1.74),
            ("600519", "SSE", "600519.SH", "贵州茅台", "guizhoumaotai", "gzmt", 1488.00, 0.86),
            ("688981", "SSE", "688981.SH", "中芯国际", "zhongxinguoji", "zxgj", 87.65, 3.21),
            ("920799", "BSE", "920799.BJ", "艾融软件", "airongruanjian", "arrj", 46.12, -0.35),
        ]
        quoted_at = now()
        db.add_all(
            [
                Stock(
                    code=code,
                    exchange=exchange,
                    symbol=symbol,
                    name=name,
                    pinyin=pinyin,
                    pinyin_initials=initials,
                    status="active",
                    last_price=price,
                    change_pct=change,
                    turnover_amount=320_000_000,
                    quote_updated_at=quoted_at,
                )
                for code, exchange, symbol, name, pinyin, initials, price, change in stocks
            ]
        )

    if not db.scalar(
        select(StrategyDefinition.id).where(
            StrategyDefinition.key == "overnight_hold"
        )
    ):
        db.add(
            StrategyDefinition(
                key="overnight_hold",
                name="一夜持股法",
                type="built_in",
                version="1.0.0",
                market="A_SHARE",
                parameter_schema={
                    "type": "object",
                    "properties": {key: {"default": value} for key, value in OVERNIGHT_DEFAULTS.items()},
                },
                signal_schema={"side": ["buy", "sell"], "required": ["symbol", "reason"]},
                required_timeframes=["1m"],
            )
        )

    if not db.scalar(
        select(StrategyDefinition.id).where(
            StrategyDefinition.key == "trading_agents_auto"
        )
    ):
        db.add(
            StrategyDefinition(
                key="trading_agents_auto",
                name="TradingAgents AI 自动组合",
                type="built_in",
                version="0.3.1",
                market="A_SHARE",
                parameter_schema={
                    "type": "object",
                    "properties": {
                        key: {"default": value}
                        for key, value in TRADING_AGENTS_DEFAULTS.items()
                    },
                },
                signal_schema={
                    "side": ["buy", "sell", "hold"],
                    "required": ["symbol", "target_weight", "reason"],
                },
                required_timeframes=["1d", "realtime"],
            )
        )

    tushare_enabled = bool(__import__("os").getenv("TUSHARE_TOKEN"))
    source_defaults = [
        {
            "provider": "akshare",
            "enabled": True,
            "healthy": False,
            "capabilities": ["stock_master", "daily", "minute", "realtime"],
            "stale_after_seconds": settings.market_stale_seconds,
            "last_error": "等待首次行情探测",
        },
        {
            "provider": "tushare",
            "enabled": tushare_enabled,
            "healthy": False,
            "capabilities": ["stock_master", "daily", "minute", "corporate_events"],
            "last_error": None if tushare_enabled else "未配置 Tushare Token",
        },
        {
            "provider": "cninfo",
            "enabled": False,
            "healthy": False,
            "capabilities": ["corporate_events"],
            "stale_after_seconds": settings.corporate_event_stale_seconds,
            "last_error": "未配置 CNINFO 抓取器",
        },
        {
            "provider": "mootdx",
            "enabled": True,
            "healthy": False,
            "capabilities": ["minute", "hour", "realtime"],
            "stale_after_seconds": settings.market_stale_seconds,
            "last_error": "等待首次行情探测",
        },
        {
            "provider": "akshare_events",
            "enabled": True,
            "healthy": False,
            "capabilities": ["corporate_events"],
            "stale_after_seconds": settings.corporate_event_stale_seconds,
            "last_error": "等待首次公告探测",
        },
    ]
    for defaults in source_defaults:
        if not db.scalar(
            select(DataSourceState.id).where(
                DataSourceState.provider == defaults["provider"]
            )
        ):
            db.add(DataSourceState(**defaults))

    if not db.scalar(select(BrokerGateway.id).limit(1)):
        db.add_all(
            [
                BrokerGateway(name="QMT 远程网关", type="qmt", platform="windows", base_url=settings.qmt_url),
                BrokerGateway(name="PTrade 券商云", type="ptrade", platform="broker_cloud", base_url=settings.ptrade_url),
                BrokerGateway(
                    name="Futu OpenD",
                    type="futu_opend",
                    platform="macos",
                    base_url=f"{settings.futu_host}:{settings.futu_port}",
                ),
            ]
        )

    db.flush()
    if not db.scalar(select(SimulationAccount.id).limit(1)):
        account = SimulationAccount(
            initial_cash=settings.simulation_initial_cash,
            cash_balance=settings.simulation_initial_cash,
            available_cash=settings.simulation_initial_cash,
            total_asset=settings.simulation_initial_cash,
            commission_rate=settings.simulation_commission_rate,
            min_commission=settings.simulation_min_commission,
            stamp_tax_rate=settings.simulation_stamp_tax_rate,
            transfer_fee_rate=settings.simulation_transfer_fee_rate,
            slippage_bps=settings.simulation_slippage_bps,
        )
        db.add(account)
        db.flush()
        db.add(
            SimulationAccountLedger(
                simulation_account_id=account.id,
                event_type="initialize",
                amount=account.initial_cash,
                balance_after=account.initial_cash,
                message="系统创建默认模拟账户",
            )
        )
        snapshot_account(db, account)

    if not db.scalar(select(RiskSettings.id).limit(1)):
        db.add_all(
            [
                RiskSettings(
                    mode="SIMULATION",
                    max_order_notional_abs=settings.simulation_max_order_notional_abs,
                    max_order_notional_pct=settings.simulation_max_order_notional_pct,
                    max_position_pct=settings.simulation_max_position_pct,
                    max_total_exposure_pct=settings.simulation_max_total_exposure_pct,
                    daily_loss_limit_pct=settings.simulation_daily_loss_limit_pct,
                    max_consecutive_errors=settings.simulation_max_consecutive_errors,
                    live_enabled=False,
                ),
                RiskSettings(
                    mode="LIVE",
                    max_order_notional_abs=settings.live_max_order_notional_abs,
                    max_order_notional_pct=settings.live_max_order_notional_pct,
                    max_position_pct=settings.live_max_position_pct,
                    max_total_exposure_pct=settings.live_max_total_exposure_pct,
                    daily_loss_limit_pct=settings.live_daily_loss_limit_pct,
                    max_consecutive_errors=settings.live_max_consecutive_errors,
                    max_daily_orders=settings.live_max_daily_orders,
                    live_enabled=settings.live_enabled,
                ),
            ]
        )
    default_account = db.scalar(
        select(SimulationAccount).order_by(SimulationAccount.id).limit(1)
    )
    if default_account:
        legacy_configs = list(
            db.scalars(
                select(StrategyConfig)
                .join(StrategyDefinition)
                .where(
                    StrategyConfig.mode == "SIMULATION",
                    StrategyConfig.simulation_account_id.is_(None),
                    StrategyDefinition.key != "trading_agents_auto",
                )
            )
        )
        for legacy_config in legacy_configs:
            legacy_config.simulation_account_id = default_account.id
    db.commit()


def snapshot_account(db: Session, account: SimulationAccount) -> AccountSnapshot:
    market_value = 0.0
    unrealized_pnl = 0.0
    for position, last_price in db.execute(
        select(Position, Stock.last_price)
        .join(Stock, Stock.id == Position.stock_id)
        .where(
            Position.account_id == account.id,
            Position.mode == "SIMULATION",
            Position.quantity > 0,
        )
    ):
        price = float(last_price or 0)
        position.market_value = position.quantity * price
        position.unrealized_pnl = (
            price - float(position.average_cost)
        ) * position.quantity
        market_value += position.market_value
        unrealized_pnl += position.unrealized_pnl
    account.unrealized_pnl = unrealized_pnl
    account.total_asset = float(account.cash_balance) + market_value
    snapshot = AccountSnapshot(
        mode="SIMULATION",
        account_id=account.id,
        cash_balance=account.cash_balance,
        available_cash=account.available_cash,
        frozen_cash=account.frozen_cash,
        market_value=market_value,
        total_asset=account.total_asset,
        realized_pnl=account.realized_pnl,
        unrealized_pnl=unrealized_pnl,
        exposure=market_value / account.total_asset if account.total_asset else 0,
        source="simulated_broker",
    )
    db.add(snapshot)
    return snapshot


def release_simulation_cash(
    db: Session,
    account: SimulationAccount,
    amount: float,
    reason: str,
    *,
    order_id: int | None = None,
) -> SimulationAccountLedger:
    if amount <= 0 or amount > account.frozen_cash:
        raise ValueError("释放金额无效或超过冻结资金")
    account.frozen_cash -= amount
    account.available_cash += amount
    ledger = SimulationAccountLedger(
        simulation_account_id=account.id,
        event_type="order_release",
        amount=amount,
        balance_after=account.cash_balance,
        related_order_id=order_id,
        message=reason,
    )
    db.add(ledger)
    db.commit()
    return ledger


def adjust_simulation_cash(
    db: Session,
    account: SimulationAccount,
    amount: float,
    reason: str,
) -> SimulationAccountLedger:
    if amount == 0 or not reason.strip():
        raise ValueError("调整金额和原因不能为空")
    if account.available_cash + amount < 0:
        raise ValueError("调整后可用资金不能为负")
    account.cash_balance += amount
    account.available_cash += amount
    account.total_asset += amount
    ledger = SimulationAccountLedger(
        simulation_account_id=account.id,
        event_type="adjustment",
        amount=amount,
        balance_after=account.cash_balance,
        message=reason,
    )
    db.add(ledger)
    snapshot_account(db, account)
    db.commit()
    return ledger


def stock_payload(stock: Stock) -> dict[str, Any]:
    return serialize(stock)


def watchlist_payload(db: Session, item: WatchlistItem) -> dict[str, Any]:
    stock = db.get(Stock, item.stock_id)
    return {"id": item.id, "stock": stock_payload(stock), "note": item.note, "created_at": item.created_at.isoformat()}


def _critical_event_symbols(
    db: Session,
    *,
    current: datetime | None = None,
    lookback_days: int = 7,
) -> set[str]:
    current = current or now()
    blocking_types = {
        "suspension",
        "resumption",
        "regulatory_investigation",
        "material_litigation",
        "shareholder_reduction",
        "earnings_warning",
        "major_announcement",
    }
    rows = db.scalars(
        select(StockEvent).where(
            StockEvent.published_at >= current - timedelta(days=lookback_days),
            StockEvent.published_at <= current,
            or_(
                StockEvent.event_type.in_(blocking_types),
                and_(
                    StockEvent.event_type == "unlock",
                    or_(
                        StockEvent.unlock_free_float_pct.is_(None),
                        StockEvent.unlock_free_float_pct > 0.05,
                    ),
                ),
            ),
        )
    ).all()
    if not rows:
        return set()
    stock_ids = {item.stock_id for item in rows}
    stocks = db.scalars(select(Stock).where(Stock.id.in_(stock_ids))).all()
    return {stock.symbol for stock in stocks}


def _select_overnight_stock(db: Session, config: StrategyConfig, *, current: datetime) -> tuple[Stock | None, dict[str, Any]]:
    parameters = {**OVERNIGHT_DEFAULTS, **(config.parameters or {})}
    include_bse = bool(parameters.get("include_bse", OVERNIGHT_DEFAULTS["include_bse"]))
    universe_stocks = db.scalars(select(Stock).order_by(Stock.symbol)).all()
    candidates = build_universe_candidates(universe_stocks, current=current, include_bse=include_bse)
    selection = select_best_candidate(
        candidates,
        parameters,
        critical_event_symbols=_critical_event_symbols(db, current=current),
    )
    summary = {
        "scanned": len(candidates),
        "accepted": len(selection.accepted),
        "rejected": len(selection.rejected),
        "selected_symbol": selection.selected["symbol"] if selection.selected else None,
        "accepted_symbols": [item["symbol"] for item in selection.accepted],
        "rejected_details": selection.rejected[:5],
    }
    if not selection.selected:
        return None, summary
    stock = db.scalar(select(Stock).where(Stock.symbol == selection.selected["symbol"]))
    return stock, summary


def execute_simulation_strategy(db: Session, config: StrategyConfig) -> StrategyRun:
    run = StrategyRun(strategy_config_id=config.id, mode=config.mode, status="running")
    db.add(run)
    db.flush()
    db.add(StrategyLog(strategy_run_id=run.id, message="开始执行策略前置检查", context={"mode": config.mode}))

    if config.mode == "LIVE":
        return _execute_live_strategy(db, config, run)

    account = (
        db.get(SimulationAccount, config.simulation_account_id)
        if config.simulation_account_id
        else db.scalar(select(SimulationAccount).order_by(SimulationAccount.id).limit(1))
    )
    risk = db.scalar(select(RiskSettings).where(RiskSettings.mode == "SIMULATION"))
    current = now()
    stock, selection_summary = _select_overnight_stock(db, config, current=current)
    if not account or not risk or not stock or not stock.last_price:
        if stock is None and account and risk:
            run.status = "completed"
            run.summary = {
                "accepted": 0,
                "rejected": selection_summary["rejected"],
                "reason": "没有候选股通过策略筛选",
                "selection": selection_summary,
            }
            run.finished_at = current
            db.add(
                StrategyLog(
                    strategy_run_id=run.id,
                    level="warning",
                    message="未找到符合条件的候选股",
                    context=selection_summary,
                )
            )
            db.commit()
            return run
        run.status = "failed"
        run.error_message = "缺少模拟账户、风险配置或候选股票行情"
        run.finished_at = current
        db.commit()
        return run

    quote_at = stock.quote_updated_at
    if not quote_at:
        return _complete_rejected_run(
            db,
            run,
            "行情时间缺失",
            stock.symbol,
            selection=selection_summary,
            retryable=True,
        )
    if quote_is_stale(quote_at, current=current, stale_after_seconds=get_settings().market_stale_seconds):
        return _complete_rejected_run(
            db,
            run,
            "行情已过期",
            stock.symbol,
            selection=selection_summary,
            retryable=True,
        )

    if config.parameters.get("event_risk_enabled", OVERNIGHT_DEFAULTS["event_risk_enabled"]):
        event_sources = list(
            db.scalars(
                select(DataSourceState).where(
                    DataSourceState.enabled.is_(True),
                    DataSourceState.healthy.is_(True),
                )
            )
        )
        event_source = max(
            (
                source
                for source in event_sources
                if "corporate_events" in (source.capabilities or [])
            ),
            key=lambda source: source.last_checked_at or datetime.min.replace(tzinfo=current.tzinfo),
            default=None,
        )
        try:
            ensure_fresh(
                "公司事件",
                updated_at=event_source.last_checked_at if event_source and event_source.healthy else None,
                stale_after_seconds=get_settings().corporate_event_stale_seconds,
                current=current,
            )
        except StaleDataError as exc:
            return _complete_rejected_run(
                db,
                run,
                str(exc),
                stock.symbol,
                "stale_event_data",
                selection=selection_summary,
                retryable=True,
            )

    max_notional = min(risk.max_order_notional_abs, account.total_asset * risk.max_order_notional_pct)
    quantity = math.floor(max_notional / stock.last_price / 100) * 100
    if quantity <= 0:
        run.status = "completed"
        run.summary = {"accepted": 0, "rejected": 1, "reason": "资金不足一手", "selection": selection_summary}
        db.add(RiskEvent(mode="SIMULATION", event_type="blocked", strategy_run_id=run.id, message="资金不足一手", context={"symbol": stock.symbol}))
        run.finished_at = now()
        db.commit()
        return run

    slip_price = stock.last_price * (1 + account.slippage_bps / 10_000)
    notional = slip_price * quantity
    commission = max(notional * account.commission_rate, account.min_commission)
    transfer_fee = notional * account.transfer_fee_rate
    total_cost = notional + commission + transfer_fee
    if total_cost > account.available_cash:
        run.status = "completed"
        run.summary = {"accepted": 0, "rejected": 1, "reason": "可用资金不足", "selection": selection_summary}
        run.finished_at = now()
        db.commit()
        return run

    position = db.scalar(
        select(Position).where(
            Position.account_id == account.id,
            Position.mode == "SIMULATION",
            Position.stock_id == stock.id,
        )
    )
    total_market_value = db.scalar(
        select(func.coalesce(func.sum(Position.market_value), 0)).where(
            Position.account_id == account.id,
            Position.mode == "SIMULATION",
        )
    ) or 0
    decision = evaluate_order(
        risk,
        order_notional=notional,
        total_asset=account.total_asset,
        position_market_value=position.market_value if position else 0,
        total_market_value=total_market_value,
        daily_pnl_pct=(account.realized_pnl + account.unrealized_pnl) / account.initial_cash
        if account.initial_cash
        else 0,
        consecutive_errors=0,
    )
    if not decision.allowed:
        return _complete_rejected_run(db, run, decision.message, stock.symbol, decision.code, selection=selection_summary)

    signal = Signal(
        strategy_run_id=run.id,
        stock_id=stock.id,
        side="buy",
        quantity=quantity,
        price_type="market",
        reason="尾盘强势候选，进入模拟仓位",
    )
    db.add(signal)
    db.flush()
    order = Order(
        account_id=account.id,
        mode="SIMULATION",
        strategy_run_id=run.id,
        signal_id=signal.id,
        stock_id=stock.id,
        side="buy",
        quantity=quantity,
        price_type="market",
        status="filled",
        submitted_at=now(),
    )
    db.add(order)
    db.flush()
    account.available_cash -= total_cost
    account.frozen_cash += total_cost
    db.add(
        SimulationAccountLedger(
            simulation_account_id=account.id,
            event_type="order_freeze",
            amount=-total_cost,
            balance_after=account.cash_balance,
            related_order_id=order.id,
            message=f"冻结模拟买入 {stock.symbol} 所需资金",
        )
    )
    fill = Fill(
        order_id=order.id,
        account_id=account.id,
        stock_id=stock.id,
        mode="SIMULATION",
        quantity=quantity,
        price=slip_price,
        commission=commission,
        transfer_fee=transfer_fee,
        slippage_amount=(slip_price - stock.last_price) * quantity,
    )
    db.add(fill)
    db.flush()

    if not position:
        position = Position(
            account_id=account.id,
            mode="SIMULATION",
            stock_id=stock.id,
            quantity=0,
            available_quantity=0,
            average_cost=0,
            market_value=0,
            unrealized_pnl=0,
        )
        db.add(position)
    old_cost = position.average_cost * position.quantity
    position.quantity += quantity
    position.available_quantity += 0  # A-share T+1: newly bought shares are unavailable today.
    position.average_cost = (old_cost + total_cost) / position.quantity
    position.market_value = position.quantity * stock.last_price
    position.unrealized_pnl = position.market_value - position.average_cost * position.quantity

    account.cash_balance -= total_cost
    account.frozen_cash -= total_cost
    account.total_asset = account.cash_balance + position.market_value
    account.unrealized_pnl = position.unrealized_pnl
    db.add(
        SimulationAccountLedger(
            simulation_account_id=account.id,
            event_type="fill",
            amount=-total_cost,
            balance_after=account.cash_balance,
            related_order_id=order.id,
            related_fill_id=fill.id,
            message=f"模拟买入 {stock.symbol} {quantity} 股",
        )
    )
    snapshot_account(db, account)
    exit_day = (now() + timedelta(days=1)).date()
    while exit_day.weekday() >= 5:
        exit_day += timedelta(days=1)
    exit_plan = {
        "trigger_type": "exit_evaluation",
        "trading_date": exit_day.isoformat(),
        "run_time": config.parameters.get("exit_evaluation_time", OVERNIGHT_DEFAULTS["exit_evaluation_time"]),
        "force_exit_time": config.parameters.get("force_exit_time", OVERNIGHT_DEFAULTS["force_exit_time"]),
        "latest_exit_time": config.parameters.get("latest_exit_time", OVERNIGHT_DEFAULTS["latest_exit_time"]),
        "symbol": stock.symbol,
        "quantity": quantity,
    }
    run.status = "completed"
    run.finished_at = now()
    run.summary = {
        "accepted": 1,
        "rejected": selection_summary["rejected"],
        "order_id": order.id,
        "symbol": stock.symbol,
        "selected_symbol": stock.symbol,
        "selection": selection_summary,
        "exit_plan": exit_plan,
    }
    db.add(StrategyLog(strategy_run_id=run.id, message="模拟订单成交", context=run.summary))
    db.add(StrategyLog(strategy_run_id=run.id, message="已生成次交易日退出计划", context=exit_plan))
    queue_notifications(
        db,
        event_type="order_success",
        severity="info",
        subject="模拟订单成交",
        payload={"order_id": order.id, "symbol": stock.symbol, "quantity": quantity},
    )
    db.commit()
    return run


def _complete_rejected_run(
    db: Session,
    run: StrategyRun,
    message: str,
    symbol: str,
    event_type: str = "blocked",
    *,
    selection: dict[str, Any] | None = None,
    retryable: bool = False,
) -> StrategyRun:
    run.status = "completed"
    run.summary = {
        "accepted": 0,
        "rejected": 1,
        "reason": message,
        "symbol": symbol,
        "selection": selection or {},
        "retryable": retryable,
    }
    run.finished_at = now()
    db.add(
        RiskEvent(
            mode=run.mode,
            event_type=event_type,
            strategy_run_id=run.id,
            message=message,
            context={"symbol": symbol, "selection": selection or {}},
        )
    )
    db.add(StrategyLog(strategy_run_id=run.id, level="warning", message=message, context={"symbol": symbol, "selection": selection or {}}))
    queue_notifications(
        db,
        event_type="risk_block",
        severity="warning",
        subject="策略订单被风控拦截",
        payload={"strategy_run_id": run.id, "symbol": symbol, "message": message},
    )
    db.commit()
    return run


def execute_simulation_exit(db: Session, config: StrategyConfig) -> StrategyRun:
    run = StrategyRun(strategy_config_id=config.id, mode=config.mode, status="running")
    db.add(run)
    db.flush()
    if config.mode != "SIMULATION":
        run.status = "failed"
        run.error_message = "真实盘退出必须由 BrokerAdapter 单独执行"
        run.finished_at = now()
        db.add(
            RiskEvent(
                mode=config.mode,
                event_type="blocked",
                strategy_run_id=run.id,
                message=run.error_message,
                context={},
            )
        )
        db.commit()
        return run

    account = (
        db.get(SimulationAccount, config.simulation_account_id)
        if config.simulation_account_id
        else db.scalar(select(SimulationAccount).order_by(SimulationAccount.id).limit(1))
    )
    risk = db.scalar(select(RiskSettings).where(RiskSettings.mode == "SIMULATION"))
    if not account or not risk or risk.emergency_stop_enabled:
        return _complete_rejected_run(db, run, "模拟账户不可用或已紧急停止", "")

    current = now()
    account_positions = list(
        db.scalars(
            select(Position).where(
                Position.account_id == account.id,
                Position.mode == "SIMULATION",
                Position.quantity > 0,
            )
        )
    )
    config_available: dict[int, int] = {}
    for position in account_positions:
        cutoff = datetime.combine(
            current.date(),
            datetime.min.time(),
            tzinfo=current.tzinfo,
        )
        previous_buy_quantity = db.scalar(
            select(func.coalesce(func.sum(Fill.quantity), 0))
            .join(Order, Fill.order_id == Order.id)
            .where(
                Fill.account_id == account.id,
                Fill.mode == "SIMULATION",
                Fill.stock_id == position.stock_id,
                Order.side == "buy",
                Fill.filled_at < cutoff,
            )
        ) or 0
        sold_quantity = db.scalar(
            select(func.coalesce(func.sum(Fill.quantity), 0))
            .join(Order, Fill.order_id == Order.id)
            .where(
                Fill.account_id == account.id,
                Fill.mode == "SIMULATION",
                Fill.stock_id == position.stock_id,
                Order.side == "sell",
                Fill.filled_at <= current,
            )
        ) or 0
        position.available_quantity = min(
            position.quantity,
            max(0, int(previous_buy_quantity - sold_quantity)),
        )
        config_buy_quantity = db.scalar(
            select(func.coalesce(func.sum(Fill.quantity), 0))
            .join(Order, Fill.order_id == Order.id)
            .join(StrategyRun, Order.strategy_run_id == StrategyRun.id)
            .where(
                Fill.account_id == account.id,
                Fill.mode == "SIMULATION",
                Fill.stock_id == position.stock_id,
                Order.side == "buy",
                StrategyRun.strategy_config_id == config.id,
                Fill.filled_at < cutoff,
            )
        ) or 0
        config_sell_quantity = db.scalar(
            select(func.coalesce(func.sum(Fill.quantity), 0))
            .join(Order, Fill.order_id == Order.id)
            .join(StrategyRun, Order.strategy_run_id == StrategyRun.id)
            .where(
                Fill.account_id == account.id,
                Fill.mode == "SIMULATION",
                Fill.stock_id == position.stock_id,
                Order.side == "sell",
                StrategyRun.strategy_config_id == config.id,
                Fill.filled_at <= current,
            )
        ) or 0
        config_available[position.id] = min(
            position.available_quantity,
            max(0, int(config_buy_quantity - config_sell_quantity)),
        )
    db.flush()

    positions = [
        position
        for position in account_positions
        if config_available.get(position.id, 0) > 0
    ]
    if not positions:
        run.status = "completed"
        run.finished_at = now()
        run.summary = {"accepted": 0, "rejected": 0, "reason": "没有可卖持仓"}
        db.add(StrategyLog(strategy_run_id=run.id, message="退出检查完成：没有可卖持仓", context={}))
        db.commit()
        return run

    stale_symbols = []
    for position in positions:
        stock = db.get(Stock, position.stock_id)
        if (
            not stock
            or not stock.last_price
            or quote_is_stale(
                stock.quote_updated_at,
                current=current,
                stale_after_seconds=get_settings().market_stale_seconds,
            )
        ):
            stale_symbols.append(stock.symbol if stock else str(position.stock_id))
    if stale_symbols:
        return _complete_rejected_run(
            db,
            run,
            f"退出行情已过期: {', '.join(stale_symbols[:5])}",
            stale_symbols[0],
            "stale_quote_data",
            retryable=True,
        )

    sold = []
    for position in positions:
        stock = db.get(Stock, position.stock_id)
        if not stock or not stock.last_price:
            db.add(
                StrategyLog(
                    strategy_run_id=run.id,
                    level="warning",
                    message="持仓行情缺失，跳过退出",
                    context={"stock_id": position.stock_id},
                )
            )
            continue
        quantity = config_available[position.id]
        fill_price = stock.last_price * (1 - account.slippage_bps / 10_000)
        notional = fill_price * quantity
        commission = max(notional * account.commission_rate, account.min_commission)
        stamp_tax = notional * account.stamp_tax_rate
        transfer_fee = notional * account.transfer_fee_rate
        proceeds = notional - commission - stamp_tax - transfer_fee
        realized_pnl = proceeds - position.average_cost * quantity

        signal = Signal(
            strategy_run_id=run.id,
            stock_id=stock.id,
            side="sell",
            quantity=quantity,
            price_type="market",
            reason="次交易日退出计划触发",
        )
        db.add(signal)
        db.flush()
        order = Order(
            account_id=account.id,
            mode="SIMULATION",
            strategy_run_id=run.id,
            signal_id=signal.id,
            stock_id=stock.id,
            side="sell",
            quantity=quantity,
            price_type="market",
            status="filled",
            submitted_at=now(),
        )
        db.add(order)
        db.flush()
        fill = Fill(
            order_id=order.id,
            account_id=account.id,
            stock_id=stock.id,
            mode="SIMULATION",
            quantity=quantity,
            price=fill_price,
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            slippage_amount=(stock.last_price - fill_price) * quantity,
        )
        db.add(fill)
        db.flush()

        position.quantity -= quantity
        position.available_quantity -= quantity
        position.market_value = position.quantity * stock.last_price
        if position.quantity == 0:
            position.average_cost = 0
            position.unrealized_pnl = 0
        account.cash_balance += proceeds
        account.available_cash += proceeds
        account.realized_pnl += realized_pnl
        db.add(
            SimulationAccountLedger(
                simulation_account_id=account.id,
                event_type="fill",
                amount=proceeds,
                balance_after=account.cash_balance,
                related_order_id=order.id,
                related_fill_id=fill.id,
                message=f"模拟卖出 {stock.symbol} {quantity} 股",
            )
        )
        sold.append({"symbol": stock.symbol, "quantity": quantity, "realized_pnl": realized_pnl})

    market_value = db.scalar(
        select(func.coalesce(func.sum(Position.market_value), 0)).where(
            Position.account_id == account.id,
            Position.mode == "SIMULATION",
        )
    ) or 0
    account.total_asset = account.cash_balance + market_value
    account.unrealized_pnl = db.scalar(
        select(func.coalesce(func.sum(Position.unrealized_pnl), 0)).where(
            Position.account_id == account.id,
            Position.mode == "SIMULATION",
        )
    ) or 0
    snapshot_account(db, account)
    run.status = "completed"
    run.finished_at = now()
    run.summary = {"accepted": len(sold), "rejected": len(positions) - len(sold), "sold": sold}
    db.add(StrategyLog(strategy_run_id=run.id, message="次交易日退出执行完成", context=run.summary))
    db.commit()
    return run


def _fail_live_run(
    db: Session,
    run: StrategyRun,
    message: str,
    *,
    event_type: str = "blocked",
    context: dict[str, Any] | None = None,
) -> StrategyRun:
    run.status = "failed"
    run.error_message = message
    run.finished_at = now()
    db.add(
        RiskEvent(
            mode="LIVE",
            event_type=event_type,
            strategy_run_id=run.id,
            message=message,
            context=context or {},
        )
    )
    queue_notifications(
        db,
        event_type="risk_block" if event_type == "blocked" else "strategy_failure",
        severity="critical",
        subject="真实盘策略执行失败",
        payload={"strategy_run_id": run.id, "message": message, **(context or {})},
    )
    db.commit()
    return run


def _execute_live_strategy(db: Session, config: StrategyConfig, run: StrategyRun) -> StrategyRun:
    app_settings = get_settings()
    if not live_runtime_is_open(app_settings):
        return _fail_live_run(db, run, "运行配置未启用真实盘，禁止下单")
    risk = db.scalar(select(RiskSettings).where(RiskSettings.mode == "LIVE"))
    if not risk or not risk.live_enabled:
        return _fail_live_run(db, run, "真实盘未启用")
    if risk.emergency_stop_enabled:
        return _fail_live_run(db, run, "真实盘已触发紧急停止")

    account = db.scalar(
        select(LiveTradingAccount)
        .where(LiveTradingAccount.enabled.is_(True))
        .order_by(LiveTradingAccount.id)
    )
    if not account:
        return _fail_live_run(db, run, "未选择已启用的真实盘账户")
    if account.read_only:
        return _fail_live_run(db, run, "真实盘账户处于只读状态")

    gateway = db.get(BrokerGateway, account.gateway_id)
    if not gateway or not gateway.enabled or not gateway.healthy:
        return _fail_live_run(db, run, "真实盘网关未启用或健康状态异常")

    snapshot = db.scalar(
        select(AccountSnapshot)
        .where(AccountSnapshot.mode == "LIVE", AccountSnapshot.account_id == account.id)
        .order_by(AccountSnapshot.captured_at.desc())
    )
    if not snapshot or snapshot.total_asset <= 0:
        return _fail_live_run(db, run, "真实盘账户资产快照缺失")
    if snapshot.exposure >= risk.max_total_exposure_pct:
        return _fail_live_run(db, run, "真实盘总仓位已达到风控上限")

    stock, selection_summary = _select_overnight_stock(db, config, current=now())
    if stock is None:
        return _fail_live_run(db, run, "没有候选股通过策略筛选", context={"selection": selection_summary})
    if not stock or not stock.last_price or not stock.quote_updated_at:
        return _fail_live_run(db, run, "真实盘候选股票行情缺失")
    if "A_SHARE" not in (account.market_permissions or ["A_SHARE"]):
        return _fail_live_run(db, run, "真实盘账户没有 A 股交易权限")
    if stock.exchange not in {"SSE", "SZSE", "BSE"}:
        return _fail_live_run(db, run, "股票市场不在真实盘权限范围内")

    current = now()
    quote_at = stock.quote_updated_at
    if quote_is_stale(quote_at, current=current, stale_after_seconds=app_settings.market_stale_seconds):
        return _fail_live_run(db, run, "真实盘行情已过期，禁止下单", context={"symbol": stock.symbol, "selection": selection_summary})

    max_notional = min(risk.max_order_notional_abs, snapshot.total_asset * risk.max_order_notional_pct)
    remaining_exposure = max(0.0, snapshot.total_asset * risk.max_total_exposure_pct - snapshot.market_value)
    max_notional = min(max_notional, remaining_exposure)
    quantity = math.floor(max_notional / stock.last_price / 100) * 100
    if quantity <= 0:
        return _fail_live_run(db, run, "真实盘风控额度不足一手", context={"symbol": stock.symbol, "selection": selection_summary})

    day_start = datetime.combine(current.date(), datetime.min.time(), tzinfo=current.tzinfo)
    daily_order_count = db.scalar(
        select(func.count(Order.id)).where(Order.mode == "LIVE", Order.created_at >= day_start)
    ) or 0
    if risk.max_daily_orders is not None and daily_order_count >= risk.max_daily_orders:
        return _fail_live_run(db, run, "真实盘当日订单数达到风控上限")
    live_position = db.scalar(
        select(Position).where(
            Position.account_id == account.id,
            Position.mode == "LIVE",
            Position.stock_id == stock.id,
        )
    )
    consecutive_errors = db.scalar(
        select(func.count(RiskEvent.id)).where(
            RiskEvent.mode == "LIVE",
            RiskEvent.event_type.in_(["broker_error", "gateway_error"]),
        )
    ) or 0
    decision = evaluate_order(
        risk,
        order_notional=stock.last_price * quantity,
        total_asset=snapshot.total_asset,
        position_market_value=live_position.market_value if live_position else 0,
        total_market_value=snapshot.market_value,
        daily_pnl_pct=(snapshot.realized_pnl + snapshot.unrealized_pnl) / snapshot.total_asset,
        consecutive_errors=consecutive_errors,
    )
    if not decision.allowed:
        return _fail_live_run(db, run, decision.message, context={"risk_code": decision.code, "selection": selection_summary})

    adapter = build_broker_adapter(
        gateway.type,
        qmt_url=app_settings.qmt_url,
        qmt_token=app_settings.qmt_token,
        ptrade_url=app_settings.ptrade_url,
        ptrade_token=app_settings.ptrade_token,
        futu_host=app_settings.futu_host,
        futu_port=app_settings.futu_port,
        futu_trd_market=app_settings.futu_trd_market,
        futu_security_firm=app_settings.futu_security_firm,
        futu_trd_env=app_settings.futu_trd_env,
        futu_unlock_password=app_settings.futu_unlock_password,
    )
    health = adapter.health()
    gateway.last_checked_at = current
    gateway.healthy = health.healthy
    gateway.last_error = None if health.healthy else health.message
    if not health.healthy:
        db.add(
            GatewayEvent(
                gateway_id=gateway.id,
                event_type="health_failed",
                message=health.message,
                context={"strategy_run_id": run.id},
            )
        )
        return _fail_live_run(db, run, "真实盘网关实时健康检查失败", context={"gateway": gateway.type})

    signal = Signal(
        strategy_run_id=run.id,
        stock_id=stock.id,
        side="buy",
        quantity=quantity,
        price_type="market",
        reason="尾盘强势候选，提交真实盘网关",
    )
    db.add(signal)
    db.flush()
    order = Order(
        account_id=account.id,
        mode="LIVE",
        strategy_run_id=run.id,
        signal_id=signal.id,
        stock_id=stock.id,
        side="buy",
        quantity=quantity,
        price_type="market",
        status="created",
    )
    db.add(order)
    db.flush()
    try:
        response = adapter.place_order(
            {
                "client_order_id": str(order.id),
                "account_id": account.account_no_masked,
                "symbol": stock.symbol,
                "side": "buy",
                "quantity": quantity,
                "price_type": "market",
            }
        )
    except (RuntimeError, OSError) as exc:
        order.status = "rejected"
        return _fail_live_run(
            db,
            run,
            f"真实盘网关拒绝下单: {exc}",
            event_type="broker_error",
            context={"order_id": order.id, "gateway": gateway.type},
        )

    broker_order_id = response.get("broker_order_id") or response.get("order_id")
    if not broker_order_id:
        order.status = "rejected"
        return _fail_live_run(
            db,
            run,
            "真实盘网关返回无效订单编号",
            event_type="broker_error",
            context={"order_id": order.id, "gateway": gateway.type},
        )
    order.broker_order_id = str(broker_order_id)
    order.status = str(response.get("status") or "submitted")
    order.submitted_at = now()
    run.status = "completed"
    run.finished_at = now()
    run.summary = {
        "accepted": 1,
        "rejected": 0,
        "order_id": order.id,
        "broker_order_id": order.broker_order_id,
        "symbol": stock.symbol,
        "selected_symbol": stock.symbol,
        "selection": selection_summary,
    }
    db.add(StrategyLog(strategy_run_id=run.id, message="真实盘订单已提交网关", context=run.summary))
    queue_notifications(
        db,
        event_type="order_success",
        severity="info",
        subject="真实盘订单已提交",
        payload=run.summary,
    )
    db.commit()
    return run


def create_backtest(db: Session, definition: StrategyDefinition, request: dict[str, Any]) -> BacktestRun:
    timeframe = request.get("timeframe", "1m")
    if definition.required_timeframes and timeframe not in definition.required_timeframes:
        raise ValueError(f"策略要求时间粒度: {', '.join(definition.required_timeframes)}")
    initial_cash = float(request.get("initial_cash", 10000))
    parameters = validate_strategy_parameters(definition.parameter_schema, request.get("parameters") or {})
    run = BacktestRun(
        strategy_definition_id=definition.id,
        strategy_version=definition.version,
        parameters=parameters,
        universe=request.get("universe") or {"include_bse": False},
        benchmark_symbol=request.get("benchmark_symbol", "000300.SH"),
        timeframe=timeframe,
        start_date=request.get("start_date", "2025-01-01"),
        end_date=request.get("end_date", "2025-12-31"),
        adjustment_mode=request.get("adjustment_mode", "qfq"),
        data_provider=request.get("data_provider", "akshare:demo-cache-v1"),
        initial_cash=initial_cash,
        cost_settings={"commission": 0.0003, "min_commission": 5, "stamp_tax": 0.0005, "slippage_bps": 5},
        status="running",
        started_at=now(),
    )
    db.add(run)
    db.flush()
    stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
    if stock:
        engine = BacktestEngine()
        bars = generated_bars(stock, 12)
        result = engine.run(
            BacktestRequest(
                symbol=stock.symbol,
                bars=bars,
                initial_cash=initial_cash,
                commission_rate=0.0003,
                min_commission=5,
                stamp_tax_rate=0.0005,
                transfer_fee_rate=0.0,
                slippage_bps=5,
                buy_index=2,
                sell_index=8,
                quantity=100,
            )
        )
        for trade in result.trades:
            db.add(
                BacktestTrade(
                    backtest_run_id=run.id,
                    stock_id=stock.id,
                    side=trade["side"],
                    quantity=trade["quantity"],
                    signal_at=datetime.fromisoformat(trade["filled_at"]),
                    filled_at=datetime.fromisoformat(trade["filled_at"]),
                    signal_price=trade["fill_price"],
                    fill_price=trade["fill_price"],
                    commission=trade.get("commission", 0),
                    stamp_tax=trade.get("stamp_tax", 0),
                    transfer_fee=trade.get("transfer_fee", 0),
                    realized_pnl=trade.get("realized_pnl"),
                    reason=trade["reason"],
                )
            )
        run.metrics = result.metrics
        curve_path = Path(__file__).resolve().parents[2] / "data" / "backtests" / f"{run.id}-equity.parquet"
        curve_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import pandas as pd

            pd.DataFrame(result.equity_curve).to_parquet(curve_path, index=False)
        except Exception:
            curve_path.write_text("\n".join(f"{row['timestamp']},{row['equity']}" for row in result.equity_curve), encoding="utf-8")
        run.equity_curve_uri = f"data/backtests/{run.id}-equity.parquet"
    run.status = "completed"
    run.finished_at = now()
    db.commit()
    return run


def generated_bars(stock: Stock, count: int = 48) -> list[dict[str, Any]]:
    base = stock.last_price or 10.0
    start = now().replace(hour=9, minute=30, second=0, microsecond=0) - timedelta(days=1)
    rows = []
    for index in range(count):
        drift = math.sin(index / 5) * 0.003 + index * 0.0002
        close = round(base * (1 + drift), 3)
        rows.append(
            {
                "symbol": stock.symbol,
                "timestamp": (start + timedelta(minutes=index)).isoformat(),
                "open": round(close * 0.999, 3),
                "high": round(close * 1.003, 3),
                "low": round(close * 0.997, 3),
                "close": close,
                "volume": 100_000 + index * 2_100,
                "amount": round(close * (100_000 + index * 2_100), 2),
                "provider": "akshare-demo",
            }
        )
    return rows


def scan_plugins(directory: str) -> list[dict[str, Any]]:
    result = []
    path = Path(directory).resolve()
    path.mkdir(parents=True, exist_ok=True)
    for file in sorted(path.glob("*.py")):
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
            metadata = None
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "STRATEGY_METADATA" for target in node.targets
                ):
                    metadata = ast.literal_eval(node.value)
            if not isinstance(metadata, dict) or not {"key", "name", "version", "parameter_schema"} <= metadata.keys():
                raise ValueError("缺少 STRATEGY_METADATA 必填字段")
            if not isinstance(metadata["parameter_schema"], dict):
                raise ValueError("parameter_schema 必须是对象")
            result.append({**metadata, "type": "plugin", "enabled": False, "validation_error": None})
        except (SyntaxError, ValueError) as exc:
            result.append(
                {
                    "key": file.stem,
                    "name": file.stem,
                    "type": "plugin",
                    "enabled": False,
                    "validation_error": str(exc),
                }
            )
    return result


def sync_plugin_definitions(db: Session, directory: str) -> list[dict[str, Any]]:
    scanned = scan_plugins(directory)
    for metadata in scanned:
        key = str(metadata["key"])
        definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == key))
        if not definition:
            definition = StrategyDefinition(
                key=key,
                name=str(metadata.get("name") or key),
                type="plugin",
                version=str(metadata.get("version") or "0"),
                parameter_schema=metadata.get("parameter_schema") or {"type": "object", "properties": {}},
                signal_schema=metadata.get("signal_schema") or {
                    "required": ["symbol", "side", "quantity", "reason"]
                },
                required_timeframes=metadata.get("required_timeframes") or [],
                enabled=False,
                validation_error=metadata.get("validation_error"),
            )
            db.add(definition)
        else:
            definition.name = str(metadata.get("name") or key)
            definition.version = str(metadata.get("version") or definition.version)
            definition.parameter_schema = metadata.get("parameter_schema") or definition.parameter_schema
            definition.required_timeframes = metadata.get("required_timeframes") or definition.required_timeframes
            definition.validation_error = metadata.get("validation_error")
            if definition.validation_error:
                definition.enabled = False
    db.commit()
    return scanned
