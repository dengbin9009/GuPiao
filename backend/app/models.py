from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, event, inspect as sa_inspect
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base

SHANGHAI = ZoneInfo("Asia/Shanghai")


def now() -> datetime:
    return datetime.now(SHANGHAI)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Administrator(Base, TimestampMixin):
    __tablename__ = "administrators"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Stock(Base, TimestampMixin):
    __tablename__ = "stocks"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(12), index=True)
    exchange: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    pinyin: Mapped[str] = mapped_column(String(128), default="")
    pinyin_initials: Mapped[str] = mapped_column(String(32), default="", index=True)
    status: Mapped[str] = mapped_column(String(24), default="active")
    last_price: Mapped[float | None] = mapped_column(Float)
    change_pct: Mapped[float | None] = mapped_column(Float)
    turnover_amount: Mapped[float | None] = mapped_column(Float)
    quote_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StockEvent(Base, TimestampMixin):
    __tablename__ = "stock_events"
    __table_args__ = (UniqueConstraint("source", "source_event_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(48))
    severity: Mapped[str] = mapped_column(String(16), default="info")
    title: Mapped[str] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(String(24))
    source_event_id: Mapped[str] = mapped_column(String(128))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unlock_free_float_pct: Mapped[float | None] = mapped_column(Float)
    raw_uri: Mapped[str | None] = mapped_column(String(512))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MarketDailyBar(Base):
    __tablename__ = "market_daily_bars"
    __table_args__ = (UniqueConstraint("stock_id", "trade_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), index=True)
    trade_date: Mapped[str] = mapped_column(String(10), index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str] = mapped_column(String(32))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), unique=True)
    note: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DataSourceState(Base):
    __tablename__ = "data_source_states"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(24), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    healthy: Mapped[bool] = mapped_column(Boolean, default=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_quote_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stale_after_seconds: Mapped[int] = mapped_column(Integer, default=15)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class StrategyDefinition(Base, TimestampMixin):
    __tablename__ = "strategy_definitions"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(24), default="built_in")
    version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    market: Mapped[str] = mapped_column(String(24), default="A_SHARE")
    parameter_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    signal_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    required_timeframes: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    validation_error: Mapped[str | None] = mapped_column(Text)


class StrategyConfig(Base, TimestampMixin):
    __tablename__ = "strategy_configs"
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_definition_id: Mapped[int] = mapped_column(ForeignKey("strategy_definitions.id"))
    name: Mapped[str] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(16), default="SIMULATION")
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    simulation_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("simulation_accounts.id")
    )


class StrategySchedule(Base, TimestampMixin):
    __tablename__ = "strategy_schedules"
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_config_id: Mapped[int] = mapped_column(ForeignKey("strategy_configs.id"))
    trigger_type: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    timezone: Mapped[str] = mapped_column(String(48), default="Asia/Shanghai")
    run_time: Mapped[str] = mapped_column(String(16))
    trading_day_only: Mapped[bool] = mapped_column(Boolean, default=True)
    misfire_policy: Mapped[str] = mapped_column(String(16), default="skip")
    last_scheduled_for: Mapped[str | None] = mapped_column(String(64))
    last_run_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_runs.id"))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StrategyRun(Base):
    __tablename__ = "strategy_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_config_id: Mapped[int] = mapped_column(ForeignKey("strategy_configs.id"))
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)


class StrategyLog(Base):
    __tablename__ = "strategy_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_run_id: Mapped[int] = mapped_column(ForeignKey("strategy_runs.id"), index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TradingAgentBatch(Base, TimestampMixin):
    __tablename__ = "trading_agent_batches"
    __table_args__ = (UniqueConstraint("strategy_config_id", "trading_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_config_id: Mapped[int] = mapped_column(
        ForeignKey("strategy_configs.id"), index=True
    )
    simulation_account_id: Mapped[int] = mapped_column(
        ForeignKey("simulation_accounts.id"), index=True
    )
    trading_date: Mapped[str] = mapped_column(String(10), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    analysis_profile: Mapped[str] = mapped_column(String(32))
    position_mapping: Mapped[str] = mapped_column(String(32))
    quick_model: Mapped[str] = mapped_column(String(64))
    deep_model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32), default="1")
    config_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    candidate_symbols: Mapped[list[str]] = mapped_column(JSON, default=list)
    holding_symbols: Mapped[list[str]] = mapped_column(JSON, default=list)
    required_symbols: Mapped[list[str]] = mapped_column(JSON, default=list)
    snapshot_sha256: Mapped[str | None] = mapped_column(String(64))
    snapshot_uri: Mapped[str | None] = mapped_column(String(512))
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    order_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    rebalance_run_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_runs.id"))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    analysis_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rebalance_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


class TradingAgentCandidateAnalysis(Base):
    __tablename__ = "trading_agent_candidate_analyses"
    __table_args__ = (UniqueConstraint("batch_id", "stock_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("trading_agent_batches.id"), index=True
    )
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"), index=True)
    rank: Mapped[int | None] = mapped_column(Integer)
    is_holding: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    rating: Mapped[str | None] = mapped_column(String(24))
    ai_target_weight: Mapped[float | None] = mapped_column(Float)
    report_uri: Mapped[str | None] = mapped_column(String(512))
    report: Mapped[str | None] = mapped_column(Text)
    reasoning: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


class TradingAgentPortfolioDecision(Base):
    __tablename__ = "trading_agent_portfolio_decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("trading_agent_batches.id"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="ready")
    position_mapping: Mapped[str] = mapped_column(String(32))
    target_weights: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    rankings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    rationale: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(64))
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BacktestRun(Base, TimestampMixin):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_definition_id: Mapped[int] = mapped_column(ForeignKey("strategy_definitions.id"))
    strategy_version: Mapped[str] = mapped_column(String(32))
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    universe: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    benchmark_symbol: Mapped[str] = mapped_column(String(24), default="000300.SH")
    timeframe: Mapped[str] = mapped_column(String(8), default="1m")
    start_date: Mapped[str] = mapped_column(String(16))
    end_date: Mapped[str] = mapped_column(String(16))
    adjustment_mode: Mapped[str] = mapped_column(String(16), default="qfq")
    data_provider: Mapped[str] = mapped_column(String(64), default="akshare")
    initial_cash: Mapped[float] = mapped_column(Float, default=10000)
    cost_settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    equity_curve_uri: Mapped[str | None] = mapped_column(String(512))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    backtest_run_id: Mapped[int] = mapped_column(ForeignKey("backtest_runs.id"), index=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"))
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    signal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    signal_price: Mapped[float] = mapped_column(Float)
    fill_price: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float, default=0)
    stamp_tax: Mapped[float] = mapped_column(Float, default=0)
    transfer_fee: Mapped[float] = mapped_column(Float, default=0)
    slippage_amount: Mapped[float] = mapped_column(Float, default=0)
    realized_pnl: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_run_id: Mapped[int] = mapped_column(ForeignKey("strategy_runs.id"))
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"))
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    price_type: Mapped[str] = mapped_column(String(16), default="market")
    limit_price: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Order(Base, TimestampMixin):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(16))
    broker_order_id: Mapped[str | None] = mapped_column(String(128))
    strategy_run_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_runs.id"))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"))
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    price_type: Mapped[str] = mapped_column(String(16), default="market")
    limit_price: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default="created")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Fill(Base):
    __tablename__ = "fills"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    account_id: Mapped[int] = mapped_column(Integer)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"))
    mode: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float, default=0)
    stamp_tax: Mapped[float] = mapped_column(Float, default=0)
    transfer_fee: Mapped[float] = mapped_column(Float, default=0)
    slippage_amount: Mapped[float] = mapped_column(Float, default=0)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("account_id", "mode", "stock_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(16))
    stock_id: Mapped[int] = mapped_column(ForeignKey("stocks.id"))
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    available_quantity: Mapped[int] = mapped_column(Integer, default=0)
    average_cost: Mapped[float] = mapped_column(Float, default=0)
    market_value: Mapped[float] = mapped_column(Float, default=0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class SimulationAccount(Base, TimestampMixin):
    __tablename__ = "simulation_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="默认模拟账户")
    initial_cash: Mapped[float] = mapped_column(Float, default=10000)
    cash_balance: Mapped[float] = mapped_column(Float, default=10000)
    available_cash: Mapped[float] = mapped_column(Float, default=10000)
    frozen_cash: Mapped[float] = mapped_column(Float, default=0)
    total_asset: Mapped[float] = mapped_column(Float, default=10000)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0)
    commission_rate: Mapped[float] = mapped_column(Float, default=0.0003)
    min_commission: Mapped[float] = mapped_column(Float, default=5)
    stamp_tax_rate: Mapped[float] = mapped_column(Float, default=0.0005)
    transfer_fee_rate: Mapped[float] = mapped_column(Float, default=0)
    slippage_bps: Mapped[float] = mapped_column(Float, default=5)
    status: Mapped[str] = mapped_column(String(24), default="active")


class SimulationAccountLedger(Base):
    __tablename__ = "simulation_account_ledgers"
    id: Mapped[int] = mapped_column(primary_key=True)
    simulation_account_id: Mapped[int] = mapped_column(ForeignKey("simulation_accounts.id"))
    event_type: Mapped[str] = mapped_column(String(32))
    amount: Mapped[float] = mapped_column(Float)
    balance_after: Mapped[float] = mapped_column(Float)
    related_order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    related_fill_id: Mapped[int | None] = mapped_column(ForeignKey("fills.id"))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LiveTradingAccount(Base, TimestampMixin):
    __tablename__ = "live_trading_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    broker: Mapped[str] = mapped_column(String(64))
    account_alias: Mapped[str] = mapped_column(String(128))
    account_no_masked: Mapped[str] = mapped_column(String(64))
    gateway_id: Mapped[int] = mapped_column(ForeignKey("broker_gateways.id"))
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    market_permissions: Mapped[list[str]] = mapped_column(JSON, default=list)
    account_capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[int] = mapped_column(Integer)
    cash_balance: Mapped[float] = mapped_column(Float)
    available_cash: Mapped[float] = mapped_column(Float)
    frozen_cash: Mapped[float] = mapped_column(Float)
    market_value: Mapped[float] = mapped_column(Float)
    total_asset: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float)
    exposure: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RiskSettings(Base):
    __tablename__ = "risk_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), unique=True)
    max_order_notional_abs: Mapped[float] = mapped_column(Float)
    max_order_notional_pct: Mapped[float] = mapped_column(Float)
    max_position_pct: Mapped[float] = mapped_column(Float)
    max_total_exposure_pct: Mapped[float] = mapped_column(Float)
    daily_loss_limit_pct: Mapped[float] = mapped_column(Float)
    max_consecutive_errors: Mapped[int] = mapped_column(Integer, default=3)
    max_daily_orders: Mapped[int | None] = mapped_column(Integer)
    live_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    emergency_stop_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class RiskEvent(Base):
    __tablename__ = "risk_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))
    event_type: Mapped[str] = mapped_column(String(32))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    strategy_run_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_runs.id"))
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BrokerGateway(Base, TimestampMixin):
    __tablename__ = "broker_gateways"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    type: Mapped[str] = mapped_column(String(32), unique=True)
    platform: Mapped[str] = mapped_column(String(24))
    base_url: Mapped[str] = mapped_column(String(512), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    healthy: Mapped[bool] = mapped_column(Boolean, default=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class GatewayEvent(Base):
    __tablename__ = "gateway_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    gateway_id: Mapped[int] = mapped_column(ForeignKey("broker_gateways.id"))
    event_type: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class NotificationChannel(Base, TimestampMixin):
    __tablename__ = "notification_channels"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    recipient: Mapped[str] = mapped_column(String(256))
    secret_ref: Mapped[str] = mapped_column(String(128))
    event_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("notification_channels.id"))
    event_type: Mapped[str] = mapped_column(String(48))
    severity: Mapped[str] = mapped_column(String(16))
    subject: Mapped[str] = mapped_column(String(256))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def _reject_audit_update(_mapper, _connection, target) -> None:
    raise ValueError(f"append-only record cannot be updated: {target.__tablename__}")


def _reject_audit_delete(_mapper, _connection, target) -> None:
    raise ValueError(f"append-only record cannot be deleted: {target.__tablename__}")


def _guard_order_update(_mapper, _connection, target: Order) -> None:
    allowed = {"status", "broker_order_id", "submitted_at", "updated_at"}
    changed = {
        attribute.key
        for attribute in sa_inspect(target).attrs
        if attribute.history.has_changes()
    }
    if changed - allowed:
        raise ValueError("append-only order audit fields cannot be updated")


for _audit_model in (Signal, Fill, BacktestTrade, SimulationAccountLedger):
    event.listen(_audit_model, "before_update", _reject_audit_update)
    event.listen(_audit_model, "before_delete", _reject_audit_delete)
event.listen(Order, "before_update", _guard_order_update)
event.listen(Order, "before_delete", _reject_audit_delete)
