# Data Model: GuPiao

## Administrator

- `id`: primary key
- `username`: unique administrator name
- `password_hash`: hashed password
- `is_active`: whether login is allowed
- `last_login_at`: last successful login time
- `created_at`, `updated_at`

## Stock

- `id`: primary key
- `code`: stock code such as `600519`
- `exchange`: `SSE`, `SZSE`, or `BSE`
- `symbol`: normalized symbol such as `600519.SH`
- `name`: Chinese stock name
- `pinyin`: full pinyin for search
- `pinyin_initials`: pinyin initials for search
- `status`: active, suspended, ST, delisted, unknown
- `last_price`, `change_pct`, `turnover_amount`
- `quote_updated_at`
- `created_at`, `updated_at`

## StockEvent

- `id`: primary key
- `stock_id`: related Stock
- `event_type`: suspension, resumption, regulatory_investigation, material_litigation, shareholder_reduction, unlock, earnings_warning, major_announcement
- `severity`: info, warning, critical
- `title`: normalized event title
- `source`: cninfo, tushare, or akshare
- `source_event_id`: provider event identifier
- `published_at`
- `effective_at`: optional event effective time
- `unlock_free_float_pct`: optional unlock percentage
- `raw_uri`: source document/page reference
- `fetched_at`
- `created_at`, `updated_at`

Unique constraint: source plus `source_event_id`. Event-risk filtering fails closed when required event data is stale or unavailable.

## WatchlistItem

- `id`: primary key
- `stock_id`: related Stock
- `note`: optional administrator note
- `created_at`

Unique constraint: one watchlist item per stock.

## DataSourceState

- `id`: primary key
- `provider`: `akshare`, `tushare`, or `cninfo`
- `enabled`: whether the provider can be selected
- `healthy`: latest health result
- `capabilities`: supported daily, minute, real-time, and corporate-event capabilities
- `last_quote_at`: latest real-time quote timestamp
- `stale_after_seconds`: configured critical quote freshness threshold
- `last_checked_at`
- `last_error`: latest error summary

## StrategyDefinition

- `id`: primary key
- `key`: stable strategy key
- `name`: display name
- `type`: built-in or plugin
- `version`: strategy version
- `market`: supported market
- `parameter_schema`: JSON schema for configuration
- `signal_schema`: JSON schema for emitted signals
- `required_timeframes`: required data timeframes such as `1m`
- `enabled`: whether it can be used
- `validation_error`: plugin validation error if any
- `created_at`, `updated_at`

## StrategyConfig

- `id`: primary key
- `strategy_definition_id`: related StrategyDefinition
- `name`: administrator-defined config name
- `mode`: `SIMULATION` or `LIVE`
- `parameters`: validated strategy parameters
- `enabled`: whether scheduled/runnable
- `created_at`, `updated_at`

## StrategySchedule

- `id`: primary key
- `strategy_config_id`: related StrategyConfig
- `trigger_type`: entry_evaluation, exit_evaluation, or custom
- `enabled`: disabled by default
- `timezone`: default `Asia/Shanghai`
- `run_time`: local exchange time
- `trading_day_only`: true for v1
- `misfire_policy`: `skip`
- `last_scheduled_for`: latest claimed schedule window
- `last_run_id`: optional related StrategyRun
- `next_run_at`: next calculated trading-day execution
- `created_at`, `updated_at`

A unique schedule-window claim prevents duplicate execution. Missed windows are logged and never replayed after the trading window.

## StrategyRun

- `id`: primary key
- `strategy_config_id`: related StrategyConfig
- `mode`: `SIMULATION` or `LIVE`
- `status`: pending, running, completed, failed, paused
- `started_at`, `finished_at`
- `summary`: JSON run summary
- `error_message`: failure summary

## StrategyLog

- `id`: primary key
- `strategy_run_id`: related StrategyRun
- `level`: info, warning, error
- `message`: human-readable log message
- `context`: JSON context
- `created_at`

## BacktestRun

- `id`: primary key
- `strategy_definition_id`: related StrategyDefinition
- `strategy_version`: immutable strategy version used by the run
- `parameters`: immutable validated parameter snapshot
- `universe`: exchanges and stock filters
- `benchmark_symbol`: default `000300.SH`
- `timeframe`: `1d` or `1m`
- `start_date`, `end_date`
- `adjustment_mode`: default `qfq`
- `data_provider`: provider and cache version
- `initial_cash`: default CNY 10,000
- `cost_settings`: commission, minimum commission, stamp tax, transfer fee, and slippage
- `status`: pending, running, completed, failed, canceled
- `metrics`: cumulative/annualized/benchmark return, maximum drawdown, Sharpe ratio, win rate, profit factor, average win/loss, turnover, and exposure
- `equity_curve_uri`: Parquet artifact URI
- `started_at`, `finished_at`
- `error_message`
- `created_at`

## BacktestTrade

- `id`: primary key
- `backtest_run_id`: related BacktestRun
- `stock_id`: related Stock
- `side`: buy or sell
- `quantity`
- `signal_at`, `filled_at`
- `signal_price`, `fill_price`
- `commission`, `stamp_tax`, `transfer_fee`, `slippage_amount`
- `realized_pnl`: populated for closing trades
- `reason`: strategy reason
- `created_at`

Historical minute bars and equity curves are stored as versioned Parquet artifacts. MySQL stores reproducibility metadata and resulting trades/metrics rather than every minute bar.

## Signal

- `id`: primary key
- `strategy_run_id`: related StrategyRun
- `stock_id`: related Stock
- `side`: buy or sell
- `quantity`: desired quantity
- `price_type`: market, limit, or strategy-defined
- `limit_price`: optional limit price
- `reason`: strategy reason
- `created_at`

## Order

- `id`: primary key
- `account_id`: related simulation or live account context
- `mode`: `SIMULATION` or `LIVE`
- `broker_order_id`: external id when available
- `strategy_run_id`: optional related StrategyRun
- `signal_id`: optional related Signal
- `stock_id`: related Stock
- `side`: buy or sell
- `quantity`: requested quantity
- `price_type`: market or limit
- `limit_price`: optional limit price
- `status`: created, blocked, submitted, partially_filled, filled, canceled, rejected, failed
- `created_at`, `submitted_at`, `updated_at`

Append-only requirement: status changes are recorded as events; order rows are not silently overwritten for audit-critical fields.

For order, fill, position, and snapshot records, `mode` is the discriminator for `account_id`: `SIMULATION` points to SimulationAccount and `LIVE` points to LiveTradingAccount.

## Fill

- `id`: primary key
- `order_id`: related Order
- `account_id`: related simulation or live account context
- `stock_id`: related Stock
- `mode`: `SIMULATION` or `LIVE`
- `quantity`: filled quantity
- `price`: fill price
- `commission`: simulated or broker-reported commission
- `stamp_tax`: simulated or broker-reported stamp tax
- `transfer_fee`: simulated or broker-reported transfer fee
- `slippage_amount`: simulated slippage amount, zero for broker-reported LIVE fills unless provided
- `filled_at`
- `created_at`

## Position

- `id`: primary key
- `account_id`: related simulation or live account context
- `mode`: `SIMULATION` or `LIVE`
- `stock_id`: related Stock
- `quantity`: current quantity
- `available_quantity`: sellable quantity
- `average_cost`: average cost
- `market_value`: latest market value
- `unrealized_pnl`: unrealized profit/loss
- `updated_at`

## SimulationAccount

- `id`: primary key
- `name`: display name, default "默认模拟账户"
- `initial_cash`: starting virtual cash, default CNY 10,000 and configurable
- `cash_balance`: current cash balance
- `available_cash`: cash available for new orders
- `frozen_cash`: cash reserved for open buy orders
- `total_asset`: cash plus market value
- `realized_pnl`: realized profit/loss
- `unrealized_pnl`: unrealized profit/loss
- `commission_rate`: default `0.0003`
- `min_commission`: default CNY 5 per order
- `stamp_tax_rate`: default `0.0005` on sells
- `transfer_fee_rate`: default `0`
- `slippage_bps`: default `5`
- `status`: active, reset_pending, disabled
- `created_at`, `updated_at`

There is one default simulation account in v1. It is created automatically on first SIMULATION use.

## SimulationAccountLedger

- `id`: primary key
- `simulation_account_id`: related SimulationAccount
- `event_type`: initialize, order_frozen, fill, release, fee, reset, adjustment
- `amount`: signed cash amount
- `balance_after`: cash balance after the event
- `related_order_id`: optional related Order
- `related_fill_id`: optional related Fill
- `message`: human-readable reason
- `created_at`

Ledger rows are append-only.

## LiveTradingAccount

- `id`: primary key
- `broker`: broker display name
- `account_alias`: local display name
- `account_no_masked`: masked broker account number
- `gateway_id`: related BrokerGateway
- `currency`: CNY
- `enabled`: whether LIVE trading is allowed for this account
- `read_only`: whether the account can only be queried
- `last_synced_at`: latest successful gateway sync
- `created_at`, `updated_at`

GuPiao does not create real broker accounts and does not store broker login passwords.

## AccountSnapshot

- `id`: primary key
- `mode`: `SIMULATION` or `LIVE`
- `account_id`: related simulation or live account context
- `cash_balance`
- `available_cash`
- `frozen_cash`
- `market_value`
- `total_asset`
- `realized_pnl`
- `unrealized_pnl`
- `exposure`
- `source`: simulated_broker, qmt_gateway, or manual_sync
- `captured_at`
- `created_at`

## RiskSettings

- `id`: primary key
- `mode`: `SIMULATION` or `LIVE`
- `max_order_notional_abs`
- `max_order_notional_pct`
- `max_position_pct`
- `max_total_exposure_pct`
- `daily_loss_limit_pct`
- `max_consecutive_errors`
- `max_daily_orders`: optional absolute count, default 5 for LIVE
- `live_enabled`
- `emergency_stop_enabled`
- `updated_at`

## RiskEvent

- `id`: primary key
- `mode`: `SIMULATION` or `LIVE`
- `event_type`: blocked, reduced, paused, emergency_stop, limit_breached
- `order_id`: optional related Order
- `strategy_run_id`: optional related StrategyRun
- `message`: human-readable reason
- `context`: JSON event details
- `created_at`

## BrokerGateway

- `id`: primary key
- `name`: gateway name
- `type`: `qmt`, `ptrade`, or `futu_opend`
- `platform`: windows, macos, linux, or broker_cloud
- `base_url`: remote gateway URL
- `enabled`: whether gateway can be used
- `healthy`: latest health result
- `last_checked_at`
- `last_error`: latest error summary
- `created_at`, `updated_at`

## GatewayEvent

- `id`: primary key
- `gateway_id`: related BrokerGateway
- `event_type`: health_check, submit_order, cancel_order, query_account, error
- `message`: human-readable event
- `context`: JSON details
- `created_at`

## NotificationChannel

- `id`: primary key
- `type`: `email` or `wecom`
- `name`: administrator display name
- `enabled`
- `recipient`: email address or Enterprise WeChat destination label
- `secret_ref`: environment/secret-manager reference, never a raw SMTP password or webhook URL in logs
- `event_types`: selected notification events
- `last_tested_at`
- `created_at`, `updated_at`

## NotificationDelivery

- `id`: primary key
- `channel_id`: related NotificationChannel
- `event_type`: order_success, order_failure, risk_block, circuit_breaker, gateway_offline, gateway_recovered, strategy_failure, daily_summary
- `severity`: info, warning, critical
- `subject`
- `payload`: rendered non-secret notification data
- `status`: pending, sent, failed, abandoned
- `attempt_count`: default maximum 3 attempts
- `last_error`
- `created_at`, `sent_at`

Notification deliveries are append-only and delivery failure does not block trading.
