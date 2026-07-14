-- Generated from app.models. Regenerate with: python scripts/generate_schema.py

SET NAMES utf8mb4;

SET FOREIGN_KEY_CHECKS = 0;


CREATE TABLE account_snapshots (
	id INTEGER NOT NULL AUTO_INCREMENT,
	mode VARCHAR(16) NOT NULL,
	account_id INTEGER NOT NULL,
	cash_balance FLOAT NOT NULL,
	available_cash FLOAT NOT NULL,
	frozen_cash FLOAT NOT NULL,
	market_value FLOAT NOT NULL,
	total_asset FLOAT NOT NULL,
	realized_pnl FLOAT NOT NULL,
	unrealized_pnl FLOAT NOT NULL,
	exposure FLOAT NOT NULL,
	source VARCHAR(32) NOT NULL,
	captured_at DATETIME NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);


CREATE TABLE administrators (
	id INTEGER NOT NULL AUTO_INCREMENT,
	username VARCHAR(64) NOT NULL,
	password_hash VARCHAR(256) NOT NULL,
	is_active BOOL NOT NULL,
	last_login_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_administrators_username ON administrators (username);


CREATE TABLE broker_gateways (
	id INTEGER NOT NULL AUTO_INCREMENT,
	name VARCHAR(128) NOT NULL,
	type VARCHAR(32) NOT NULL,
	platform VARCHAR(24) NOT NULL,
	base_url VARCHAR(512) NOT NULL,
	enabled BOOL NOT NULL,
	healthy BOOL NOT NULL,
	last_checked_at DATETIME,
	last_error TEXT,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (type)
);


CREATE TABLE data_source_states (
	id INTEGER NOT NULL AUTO_INCREMENT,
	provider VARCHAR(24) NOT NULL,
	enabled BOOL NOT NULL,
	healthy BOOL NOT NULL,
	capabilities JSON NOT NULL,
	last_quote_at DATETIME,
	stale_after_seconds INTEGER NOT NULL,
	last_checked_at DATETIME,
	last_error TEXT,
	PRIMARY KEY (id),
	UNIQUE (provider)
);


CREATE TABLE notification_channels (
	id INTEGER NOT NULL AUTO_INCREMENT,
	type VARCHAR(16) NOT NULL,
	name VARCHAR(128) NOT NULL,
	enabled BOOL NOT NULL,
	recipient VARCHAR(256) NOT NULL,
	secret_ref VARCHAR(128) NOT NULL,
	event_types JSON NOT NULL,
	last_tested_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);


CREATE TABLE risk_settings (
	id INTEGER NOT NULL AUTO_INCREMENT,
	mode VARCHAR(16) NOT NULL,
	max_order_notional_abs FLOAT NOT NULL,
	max_order_notional_pct FLOAT NOT NULL,
	max_position_pct FLOAT NOT NULL,
	max_total_exposure_pct FLOAT NOT NULL,
	daily_loss_limit_pct FLOAT NOT NULL,
	max_consecutive_errors INTEGER NOT NULL,
	max_daily_orders INTEGER,
	live_enabled BOOL NOT NULL,
	emergency_stop_enabled BOOL NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (mode)
);


CREATE TABLE simulation_accounts (
	id INTEGER NOT NULL AUTO_INCREMENT,
	name VARCHAR(128) NOT NULL,
	initial_cash FLOAT NOT NULL,
	cash_balance FLOAT NOT NULL,
	available_cash FLOAT NOT NULL,
	frozen_cash FLOAT NOT NULL,
	total_asset FLOAT NOT NULL,
	realized_pnl FLOAT NOT NULL,
	unrealized_pnl FLOAT NOT NULL,
	commission_rate FLOAT NOT NULL,
	min_commission FLOAT NOT NULL,
	stamp_tax_rate FLOAT NOT NULL,
	transfer_fee_rate FLOAT NOT NULL,
	slippage_bps FLOAT NOT NULL,
	status VARCHAR(24) NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);


CREATE TABLE stocks (
	id INTEGER NOT NULL AUTO_INCREMENT,
	code VARCHAR(12) NOT NULL,
	exchange VARCHAR(16) NOT NULL,
	symbol VARCHAR(24) NOT NULL,
	name VARCHAR(64) NOT NULL,
	pinyin VARCHAR(128) NOT NULL,
	pinyin_initials VARCHAR(32) NOT NULL,
	status VARCHAR(24) NOT NULL,
	last_price FLOAT,
	change_pct FLOAT,
	turnover_amount FLOAT,
	quote_updated_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX ix_stocks_code ON stocks (code);

CREATE UNIQUE INDEX ix_stocks_symbol ON stocks (symbol);

CREATE INDEX ix_stocks_name ON stocks (name);

CREATE INDEX ix_stocks_pinyin_initials ON stocks (pinyin_initials);


CREATE TABLE strategy_definitions (
	id INTEGER NOT NULL AUTO_INCREMENT,
	`key` VARCHAR(64) NOT NULL,
	name VARCHAR(128) NOT NULL,
	type VARCHAR(24) NOT NULL,
	version VARCHAR(32) NOT NULL,
	market VARCHAR(24) NOT NULL,
	parameter_schema JSON NOT NULL,
	signal_schema JSON NOT NULL,
	required_timeframes JSON NOT NULL,
	enabled BOOL NOT NULL,
	validation_error TEXT,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (`key`)
);


CREATE TABLE backtest_runs (
	id INTEGER NOT NULL AUTO_INCREMENT,
	strategy_definition_id INTEGER NOT NULL,
	strategy_version VARCHAR(32) NOT NULL,
	parameters JSON NOT NULL,
	universe JSON NOT NULL,
	benchmark_symbol VARCHAR(24) NOT NULL,
	timeframe VARCHAR(8) NOT NULL,
	start_date VARCHAR(16) NOT NULL,
	end_date VARCHAR(16) NOT NULL,
	adjustment_mode VARCHAR(16) NOT NULL,
	data_provider VARCHAR(64) NOT NULL,
	initial_cash FLOAT NOT NULL,
	cost_settings JSON NOT NULL,
	status VARCHAR(24) NOT NULL,
	metrics JSON NOT NULL,
	equity_curve_uri VARCHAR(512),
	started_at DATETIME,
	finished_at DATETIME,
	error_message TEXT,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(strategy_definition_id) REFERENCES strategy_definitions (id)
);


CREATE TABLE gateway_events (
	id INTEGER NOT NULL AUTO_INCREMENT,
	gateway_id INTEGER NOT NULL,
	event_type VARCHAR(32) NOT NULL,
	message TEXT NOT NULL,
	context JSON NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(gateway_id) REFERENCES broker_gateways (id)
);


CREATE TABLE live_trading_accounts (
	id INTEGER NOT NULL AUTO_INCREMENT,
	broker VARCHAR(64) NOT NULL,
	account_alias VARCHAR(128) NOT NULL,
	account_no_masked VARCHAR(64) NOT NULL,
	gateway_id INTEGER NOT NULL,
	currency VARCHAR(8) NOT NULL,
	market_permissions JSON NOT NULL,
	account_capabilities JSON NOT NULL,
	enabled BOOL NOT NULL,
	read_only BOOL NOT NULL,
	last_synced_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(gateway_id) REFERENCES broker_gateways (id)
);


CREATE TABLE market_daily_bars (
	id INTEGER NOT NULL AUTO_INCREMENT,
	stock_id INTEGER NOT NULL,
	trade_date VARCHAR(10) NOT NULL,
	open FLOAT NOT NULL,
	high FLOAT NOT NULL,
	low FLOAT NOT NULL,
	close FLOAT NOT NULL,
	volume FLOAT NOT NULL,
	amount FLOAT NOT NULL,
	source VARCHAR(32) NOT NULL,
	captured_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (stock_id, trade_date),
	FOREIGN KEY(stock_id) REFERENCES stocks (id)
);

CREATE INDEX ix_market_daily_bars_stock_id ON market_daily_bars (stock_id);

CREATE INDEX ix_market_daily_bars_trade_date ON market_daily_bars (trade_date);


CREATE TABLE notification_deliveries (
	id INTEGER NOT NULL AUTO_INCREMENT,
	channel_id INTEGER NOT NULL,
	event_type VARCHAR(48) NOT NULL,
	severity VARCHAR(16) NOT NULL,
	subject VARCHAR(256) NOT NULL,
	payload JSON NOT NULL,
	status VARCHAR(24) NOT NULL,
	attempt_count INTEGER NOT NULL,
	last_error TEXT,
	created_at DATETIME NOT NULL,
	sent_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(channel_id) REFERENCES notification_channels (id)
);


CREATE TABLE positions (
	id INTEGER NOT NULL AUTO_INCREMENT,
	account_id INTEGER NOT NULL,
	mode VARCHAR(16) NOT NULL,
	stock_id INTEGER NOT NULL,
	quantity INTEGER NOT NULL,
	available_quantity INTEGER NOT NULL,
	average_cost FLOAT NOT NULL,
	market_value FLOAT NOT NULL,
	unrealized_pnl FLOAT NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (account_id, mode, stock_id),
	FOREIGN KEY(stock_id) REFERENCES stocks (id)
);


CREATE TABLE stock_events (
	id INTEGER NOT NULL AUTO_INCREMENT,
	stock_id INTEGER NOT NULL,
	event_type VARCHAR(48) NOT NULL,
	severity VARCHAR(16) NOT NULL,
	title VARCHAR(256) NOT NULL,
	source VARCHAR(24) NOT NULL,
	source_event_id VARCHAR(128) NOT NULL,
	published_at DATETIME NOT NULL,
	effective_at DATETIME,
	unlock_free_float_pct FLOAT,
	raw_uri VARCHAR(512),
	fetched_at DATETIME NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (source, source_event_id),
	FOREIGN KEY(stock_id) REFERENCES stocks (id)
);

CREATE INDEX ix_stock_events_stock_id ON stock_events (stock_id);


CREATE TABLE strategy_configs (
	id INTEGER NOT NULL AUTO_INCREMENT,
	strategy_definition_id INTEGER NOT NULL,
	name VARCHAR(128) NOT NULL,
	mode VARCHAR(16) NOT NULL,
	parameters JSON NOT NULL,
	enabled BOOL NOT NULL,
	simulation_account_id INTEGER,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(strategy_definition_id) REFERENCES strategy_definitions (id),
	FOREIGN KEY(simulation_account_id) REFERENCES simulation_accounts (id)
);


CREATE TABLE watchlist_items (
	id INTEGER NOT NULL AUTO_INCREMENT,
	stock_id INTEGER NOT NULL,
	note VARCHAR(256),
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (stock_id),
	FOREIGN KEY(stock_id) REFERENCES stocks (id)
);


CREATE TABLE backtest_trades (
	id INTEGER NOT NULL AUTO_INCREMENT,
	backtest_run_id INTEGER NOT NULL,
	stock_id INTEGER NOT NULL,
	side VARCHAR(8) NOT NULL,
	quantity INTEGER NOT NULL,
	signal_at DATETIME NOT NULL,
	filled_at DATETIME NOT NULL,
	signal_price FLOAT NOT NULL,
	fill_price FLOAT NOT NULL,
	commission FLOAT NOT NULL,
	stamp_tax FLOAT NOT NULL,
	transfer_fee FLOAT NOT NULL,
	slippage_amount FLOAT NOT NULL,
	realized_pnl FLOAT,
	reason TEXT NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(backtest_run_id) REFERENCES backtest_runs (id),
	FOREIGN KEY(stock_id) REFERENCES stocks (id)
);

CREATE INDEX ix_backtest_trades_backtest_run_id ON backtest_trades (backtest_run_id);


CREATE TABLE strategy_runs (
	id INTEGER NOT NULL AUTO_INCREMENT,
	strategy_config_id INTEGER NOT NULL,
	mode VARCHAR(16) NOT NULL,
	status VARCHAR(24) NOT NULL,
	started_at DATETIME NOT NULL,
	finished_at DATETIME,
	summary JSON NOT NULL,
	error_message TEXT,
	PRIMARY KEY (id),
	FOREIGN KEY(strategy_config_id) REFERENCES strategy_configs (id)
);


CREATE TABLE signals (
	id INTEGER NOT NULL AUTO_INCREMENT,
	strategy_run_id INTEGER NOT NULL,
	stock_id INTEGER NOT NULL,
	side VARCHAR(8) NOT NULL,
	quantity INTEGER NOT NULL,
	price_type VARCHAR(16) NOT NULL,
	limit_price FLOAT,
	reason TEXT NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(strategy_run_id) REFERENCES strategy_runs (id),
	FOREIGN KEY(stock_id) REFERENCES stocks (id)
);


CREATE TABLE strategy_logs (
	id INTEGER NOT NULL AUTO_INCREMENT,
	strategy_run_id INTEGER NOT NULL,
	level VARCHAR(16) NOT NULL,
	message TEXT NOT NULL,
	context JSON NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(strategy_run_id) REFERENCES strategy_runs (id)
);

CREATE INDEX ix_strategy_logs_strategy_run_id ON strategy_logs (strategy_run_id);


CREATE TABLE strategy_schedules (
	id INTEGER NOT NULL AUTO_INCREMENT,
	strategy_config_id INTEGER NOT NULL,
	trigger_type VARCHAR(32) NOT NULL,
	enabled BOOL NOT NULL,
	timezone VARCHAR(48) NOT NULL,
	run_time VARCHAR(16) NOT NULL,
	trading_day_only BOOL NOT NULL,
	misfire_policy VARCHAR(16) NOT NULL,
	last_scheduled_for VARCHAR(64),
	last_run_id INTEGER,
	next_run_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(strategy_config_id) REFERENCES strategy_configs (id),
	FOREIGN KEY(last_run_id) REFERENCES strategy_runs (id)
);


CREATE TABLE trading_agent_batches (
	id INTEGER NOT NULL AUTO_INCREMENT,
	strategy_config_id INTEGER NOT NULL,
	simulation_account_id INTEGER NOT NULL,
	trading_date VARCHAR(10) NOT NULL,
	status VARCHAR(24) NOT NULL,
	analysis_profile VARCHAR(32) NOT NULL,
	position_mapping VARCHAR(32) NOT NULL,
	quick_model VARCHAR(64) NOT NULL,
	deep_model VARCHAR(64) NOT NULL,
	prompt_version VARCHAR(32) NOT NULL,
	config_fingerprint VARCHAR(64),
	candidate_symbols JSON NOT NULL,
	holding_symbols JSON NOT NULL,
	required_symbols JSON NOT NULL,
	snapshot_sha256 VARCHAR(64),
	snapshot_uri VARCHAR(512),
	llm_calls INTEGER NOT NULL,
	tokens_in INTEGER NOT NULL,
	tokens_out INTEGER NOT NULL,
	order_ids JSON NOT NULL,
	rebalance_run_id INTEGER,
	lease_until DATETIME,
	worker_id VARCHAR(128),
	analysis_deadline DATETIME NOT NULL,
	rebalance_after DATETIME NOT NULL,
	started_at DATETIME,
	completed_at DATETIME,
	error_message TEXT,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (strategy_config_id, trading_date),
	FOREIGN KEY(strategy_config_id) REFERENCES strategy_configs (id),
	FOREIGN KEY(simulation_account_id) REFERENCES simulation_accounts (id),
	FOREIGN KEY(rebalance_run_id) REFERENCES strategy_runs (id)
);

CREATE INDEX ix_trading_agent_batches_config_fingerprint ON trading_agent_batches (config_fingerprint);

CREATE INDEX ix_trading_agent_batches_simulation_account_id ON trading_agent_batches (simulation_account_id);

CREATE INDEX ix_trading_agent_batches_strategy_config_id ON trading_agent_batches (strategy_config_id);

CREATE INDEX ix_trading_agent_batches_trading_date ON trading_agent_batches (trading_date);


CREATE TABLE orders (
	id INTEGER NOT NULL AUTO_INCREMENT,
	account_id INTEGER NOT NULL,
	mode VARCHAR(16) NOT NULL,
	broker_order_id VARCHAR(128),
	strategy_run_id INTEGER,
	signal_id INTEGER,
	stock_id INTEGER NOT NULL,
	side VARCHAR(8) NOT NULL,
	quantity INTEGER NOT NULL,
	price_type VARCHAR(16) NOT NULL,
	limit_price FLOAT,
	status VARCHAR(32) NOT NULL,
	submitted_at DATETIME,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(strategy_run_id) REFERENCES strategy_runs (id),
	FOREIGN KEY(signal_id) REFERENCES signals (id),
	FOREIGN KEY(stock_id) REFERENCES stocks (id)
);


CREATE TABLE trading_agent_candidate_analyses (
	id INTEGER NOT NULL AUTO_INCREMENT,
	batch_id INTEGER NOT NULL,
	stock_id INTEGER NOT NULL,
	`rank` INTEGER,
	is_holding BOOL NOT NULL,
	status VARCHAR(24) NOT NULL,
	rating VARCHAR(24),
	ai_target_weight FLOAT,
	report_uri VARCHAR(512),
	report TEXT,
	reasoning TEXT,
	stats JSON NOT NULL,
	started_at DATETIME,
	finished_at DATETIME,
	error_message TEXT,
	PRIMARY KEY (id),
	UNIQUE (batch_id, stock_id),
	FOREIGN KEY(batch_id) REFERENCES trading_agent_batches (id),
	FOREIGN KEY(stock_id) REFERENCES stocks (id)
);

CREATE INDEX ix_trading_agent_candidate_analyses_stock_id ON trading_agent_candidate_analyses (stock_id);

CREATE INDEX ix_trading_agent_candidate_analyses_batch_id ON trading_agent_candidate_analyses (batch_id);


CREATE TABLE trading_agent_portfolio_decisions (
	id INTEGER NOT NULL AUTO_INCREMENT,
	batch_id INTEGER NOT NULL,
	status VARCHAR(24) NOT NULL,
	position_mapping VARCHAR(32) NOT NULL,
	target_weights JSON NOT NULL,
	rankings JSON NOT NULL,
	rationale TEXT NOT NULL,
	model VARCHAR(64) NOT NULL,
	llm_calls INTEGER NOT NULL,
	tokens_in INTEGER NOT NULL,
	tokens_out INTEGER NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(batch_id) REFERENCES trading_agent_batches (id)
);

CREATE UNIQUE INDEX ix_trading_agent_portfolio_decisions_batch_id ON trading_agent_portfolio_decisions (batch_id);


CREATE TABLE fills (
	id INTEGER NOT NULL AUTO_INCREMENT,
	order_id INTEGER NOT NULL,
	account_id INTEGER NOT NULL,
	stock_id INTEGER NOT NULL,
	mode VARCHAR(16) NOT NULL,
	quantity INTEGER NOT NULL,
	price FLOAT NOT NULL,
	commission FLOAT NOT NULL,
	stamp_tax FLOAT NOT NULL,
	transfer_fee FLOAT NOT NULL,
	slippage_amount FLOAT NOT NULL,
	filled_at DATETIME NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(order_id) REFERENCES orders (id),
	FOREIGN KEY(stock_id) REFERENCES stocks (id)
);


CREATE TABLE risk_events (
	id INTEGER NOT NULL AUTO_INCREMENT,
	mode VARCHAR(16) NOT NULL,
	event_type VARCHAR(32) NOT NULL,
	order_id INTEGER,
	strategy_run_id INTEGER,
	message TEXT NOT NULL,
	context JSON NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(order_id) REFERENCES orders (id),
	FOREIGN KEY(strategy_run_id) REFERENCES strategy_runs (id)
);


CREATE TABLE simulation_account_ledgers (
	id INTEGER NOT NULL AUTO_INCREMENT,
	simulation_account_id INTEGER NOT NULL,
	event_type VARCHAR(32) NOT NULL,
	amount FLOAT NOT NULL,
	balance_after FLOAT NOT NULL,
	related_order_id INTEGER,
	related_fill_id INTEGER,
	message TEXT NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(simulation_account_id) REFERENCES simulation_accounts (id),
	FOREIGN KEY(related_order_id) REFERENCES orders (id),
	FOREIGN KEY(related_fill_id) REFERENCES fills (id)
);

SET FOREIGN_KEY_CHECKS = 1;
