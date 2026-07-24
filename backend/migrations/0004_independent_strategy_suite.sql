-- GuPiao 八套独立模拟策略增量迁移（MySQL 8.4）
-- 执行前必须备份数据库。本迁移只增加字段和表，不删除历史数据。

SET NAMES utf8mb4;

ALTER TABLE stocks
  ADD COLUMN instrument_type VARCHAR(16) NOT NULL DEFAULT 'STOCK',
  ADD COLUMN lot_size INTEGER NOT NULL DEFAULT 100,
  ADD COLUMN settlement_days INTEGER NOT NULL DEFAULT 1;

ALTER TABLE market_daily_bars
  ADD COLUMN adjusted_close FLOAT NULL,
  ADD COLUMN adjustment_factor FLOAT NULL,
  ADD COLUMN quality_status VARCHAR(24) NOT NULL DEFAULT 'valid';

ALTER TABLE strategy_position_lots
  ADD COLUMN metadata JSON NULL;

CREATE TABLE market_daily_metrics (
  id INTEGER NOT NULL AUTO_INCREMENT,
  stock_id INTEGER NOT NULL,
  trade_date VARCHAR(10) NOT NULL,
  pe_ttm FLOAT NULL,
  pb FLOAT NULL,
  dividend_yield FLOAT NULL,
  total_market_value FLOAT NULL,
  float_market_value FLOAT NULL,
  source VARCHAR(32) NOT NULL,
  captured_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_market_daily_metric_stock_date (stock_id, trade_date),
  KEY ix_market_daily_metrics_stock (stock_id),
  KEY ix_market_daily_metrics_date (trade_date),
  CONSTRAINT fk_market_daily_metrics_stock FOREIGN KEY (stock_id) REFERENCES stocks(id)
);

CREATE TABLE financial_report_snapshots (
  id INTEGER NOT NULL AUTO_INCREMENT,
  stock_id INTEGER NOT NULL,
  report_period VARCHAR(10) NOT NULL,
  report_type VARCHAR(24) NOT NULL,
  announcement_date VARCHAR(10) NOT NULL,
  actual_announcement_date VARCHAR(10) NOT NULL,
  available_on VARCHAR(10) NOT NULL,
  eps FLOAT NULL,
  roe FLOAT NULL,
  gross_margin FLOAT NULL,
  operating_cash_flow FLOAT NULL,
  net_profit FLOAT NULL,
  revenue FLOAT NULL,
  total_assets FLOAT NULL,
  total_liabilities FLOAT NULL,
  source VARCHAR(32) NOT NULL,
  fetched_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_financial_snapshot_stock_period_announcement (
    stock_id, report_period, actual_announcement_date
  ),
  KEY ix_financial_snapshot_available (available_on),
  CONSTRAINT fk_financial_snapshot_stock FOREIGN KEY (stock_id) REFERENCES stocks(id)
);

CREATE TABLE quant_strategy_tasks (
  id INTEGER NOT NULL AUTO_INCREMENT,
  strategy_config_id INTEGER NOT NULL,
  simulation_account_id INTEGER NOT NULL,
  task_type VARCHAR(32) NOT NULL,
  trading_date VARCHAR(10) NOT NULL,
  idempotency_key VARCHAR(160) NOT NULL,
  status VARCHAR(24) NOT NULL,
  payload JSON NOT NULL,
  result JSON NOT NULL,
  lease_until DATETIME NULL,
  next_retry_at DATETIME NULL,
  worker_id VARCHAR(128) NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  deadline_at DATETIME NULL,
  started_at DATETIME NULL,
  completed_at DATETIME NULL,
  error_message TEXT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_quant_strategy_task_idempotency (idempotency_key),
  KEY ix_quant_strategy_task_claim (status, lease_until),
  CONSTRAINT fk_quant_strategy_task_config FOREIGN KEY (strategy_config_id) REFERENCES strategy_configs(id),
  CONSTRAINT fk_quant_strategy_task_account FOREIGN KEY (simulation_account_id) REFERENCES simulation_accounts(id)
);

CREATE TABLE quant_portfolio_decisions (
  id INTEGER NOT NULL AUTO_INCREMENT,
  strategy_run_id INTEGER NULL,
  strategy_config_id INTEGER NOT NULL,
  simulation_account_id INTEGER NOT NULL,
  trading_date VARCHAR(10) NOT NULL,
  decision_type VARCHAR(24) NOT NULL,
  status VARCHAR(24) NOT NULL,
  data_as_of DATETIME NOT NULL,
  snapshot_sha256 VARCHAR(64) NULL,
  snapshot JSON NOT NULL,
  config_fingerprint VARCHAR(64) NOT NULL,
  strategy_version VARCHAR(32) NOT NULL,
  data_version VARCHAR(32) NOT NULL,
  target_weights JSON NOT NULL,
  order_ids JSON NOT NULL,
  error_message TEXT NULL,
  created_at DATETIME NOT NULL,
  completed_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_quant_decision_config_date_type_fingerprint (
    strategy_config_id, trading_date, decision_type, config_fingerprint
  ),
  UNIQUE KEY uq_quant_decision_run (strategy_run_id),
  KEY ix_quant_decision_account (simulation_account_id),
  KEY ix_quant_decision_fingerprint (config_fingerprint),
  CONSTRAINT fk_quant_decision_run FOREIGN KEY (strategy_run_id) REFERENCES strategy_runs(id),
  CONSTRAINT fk_quant_decision_config FOREIGN KEY (strategy_config_id) REFERENCES strategy_configs(id),
  CONSTRAINT fk_quant_decision_account FOREIGN KEY (simulation_account_id) REFERENCES simulation_accounts(id)
);

CREATE TABLE quant_candidate_scores (
  id INTEGER NOT NULL AUTO_INCREMENT,
  decision_id INTEGER NOT NULL,
  stock_id INTEGER NOT NULL,
  status VARCHAR(24) NOT NULL,
  `rank` INTEGER NULL,
  features JSON NOT NULL,
  score FLOAT NULL,
  target_weight FLOAT NULL,
  rejection_reasons JSON NOT NULL,
  created_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_quant_candidate_decision_stock (decision_id, stock_id),
  KEY ix_quant_candidate_stock (stock_id),
  CONSTRAINT fk_quant_candidate_decision FOREIGN KEY (decision_id) REFERENCES quant_portfolio_decisions(id),
  CONSTRAINT fk_quant_candidate_stock FOREIGN KEY (stock_id) REFERENCES stocks(id)
);

CREATE TABLE strategy_risk_profiles (
  id INTEGER NOT NULL AUTO_INCREMENT,
  strategy_config_id INTEGER NOT NULL,
  daily_loss_limit_pct FLOAT NOT NULL DEFAULT 0.02,
  max_drawdown_pct FLOAT NOT NULL DEFAULT 0.15,
  max_consecutive_errors INTEGER NOT NULL DEFAULT 3,
  max_daily_orders INTEGER NOT NULL DEFAULT 30,
  max_order_notional_pct FLOAT NOT NULL DEFAULT 0.35,
  emergency_stop_enabled BOOL NOT NULL DEFAULT FALSE,
  consecutive_errors INTEGER NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_strategy_risk_config (strategy_config_id),
  CONSTRAINT fk_strategy_risk_config FOREIGN KEY (strategy_config_id) REFERENCES strategy_configs(id)
);

CREATE TABLE strategy_performance_daily (
  id INTEGER NOT NULL AUTO_INCREMENT,
  strategy_config_id INTEGER NOT NULL,
  simulation_account_id INTEGER NOT NULL,
  trading_date VARCHAR(10) NOT NULL,
  cash_balance FLOAT NOT NULL,
  market_value FLOAT NOT NULL,
  total_asset FLOAT NOT NULL,
  daily_return FLOAT NOT NULL DEFAULT 0,
  cumulative_return FLOAT NOT NULL DEFAULT 0,
  drawdown FLOAT NOT NULL DEFAULT 0,
  exposure FLOAT NOT NULL DEFAULT 0,
  captured_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_strategy_performance_config_date (strategy_config_id, trading_date),
  KEY ix_strategy_performance_account (simulation_account_id),
  CONSTRAINT fk_strategy_performance_config FOREIGN KEY (strategy_config_id) REFERENCES strategy_configs(id),
  CONSTRAINT fk_strategy_performance_account FOREIGN KEY (simulation_account_id) REFERENCES simulation_accounts(id)
);

CREATE TABLE strategy_backtest_qualifications (
  id INTEGER NOT NULL AUTO_INCREMENT,
  strategy_config_id INTEGER NOT NULL,
  backtest_run_id INTEGER NULL,
  config_fingerprint VARCHAR(64) NOT NULL,
  strategy_version VARCHAR(32) NOT NULL,
  data_version VARCHAR(32) NOT NULL,
  trading_days INTEGER NOT NULL,
  data_completeness FLOAT NOT NULL,
  out_of_sample_annualized_return FLOAT NOT NULL,
  sharpe_ratio FLOAT NOT NULL,
  max_drawdown FLOAT NOT NULL,
  trade_count INTEGER NOT NULL,
  qualified BOOL NOT NULL DEFAULT FALSE,
  evaluated_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  KEY ix_strategy_backtest_config (strategy_config_id),
  KEY ix_strategy_backtest_fingerprint (config_fingerprint),
  CONSTRAINT fk_strategy_backtest_config FOREIGN KEY (strategy_config_id) REFERENCES strategy_configs(id),
  CONSTRAINT fk_strategy_backtest_run FOREIGN KEY (backtest_run_id) REFERENCES backtest_runs(id)
);

CREATE TABLE strategy_dry_run_approvals (
  id INTEGER NOT NULL AUTO_INCREMENT,
  strategy_config_id INTEGER NOT NULL,
  decision_id INTEGER NOT NULL,
  config_fingerprint VARCHAR(64) NOT NULL,
  strategy_version VARCHAR(32) NOT NULL,
  data_version VARCHAR(32) NOT NULL,
  validated_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_strategy_dry_run_decision (decision_id),
  KEY ix_strategy_dry_run_config_fingerprint (strategy_config_id, config_fingerprint),
  CONSTRAINT fk_strategy_dry_run_config FOREIGN KEY (strategy_config_id) REFERENCES strategy_configs(id),
  CONSTRAINT fk_strategy_dry_run_decision FOREIGN KEY (decision_id) REFERENCES quant_portfolio_decisions(id)
);
