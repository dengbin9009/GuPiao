-- GuPiao TradingAgents 自动模拟交易增量迁移（MySQL 8.4）
-- 执行前必须备份数据库；本迁移不会删除已有表或数据。

SET NAMES utf8mb4;

ALTER TABLE strategy_configs
  ADD COLUMN simulation_account_id INTEGER NULL,
  ADD CONSTRAINT fk_strategy_configs_simulation_account
    FOREIGN KEY (simulation_account_id) REFERENCES simulation_accounts(id);

CREATE TABLE market_daily_bars (
  id INTEGER NOT NULL AUTO_INCREMENT,
  stock_id INTEGER NOT NULL,
  trade_date VARCHAR(10) NOT NULL,
  open FLOAT NOT NULL,
  high FLOAT NOT NULL,
  low FLOAT NOT NULL,
  close FLOAT NOT NULL,
  volume FLOAT NOT NULL DEFAULT 0,
  amount FLOAT NOT NULL DEFAULT 0,
  source VARCHAR(32) NOT NULL,
  captured_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_market_daily_bars_stock_date (stock_id, trade_date),
  KEY ix_market_daily_bars_stock_id (stock_id),
  KEY ix_market_daily_bars_trade_date (trade_date),
  CONSTRAINT fk_market_daily_bars_stock FOREIGN KEY (stock_id) REFERENCES stocks(id)
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
  config_fingerprint VARCHAR(64) NULL,
  candidate_symbols JSON NOT NULL,
  holding_symbols JSON NOT NULL,
  required_symbols JSON NOT NULL,
  snapshot_sha256 VARCHAR(64) NULL,
  snapshot_uri VARCHAR(512) NULL,
  llm_calls INTEGER NOT NULL DEFAULT 0,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  order_ids JSON NOT NULL,
  rebalance_run_id INTEGER NULL,
  lease_until DATETIME NULL,
  worker_id VARCHAR(128) NULL,
  analysis_deadline DATETIME NOT NULL,
  rebalance_after DATETIME NOT NULL,
  started_at DATETIME NULL,
  completed_at DATETIME NULL,
  error_message TEXT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_trading_agent_batch_config_date (strategy_config_id, trading_date),
  KEY ix_trading_agent_batches_account (simulation_account_id),
  KEY ix_trading_agent_batches_date (trading_date),
  KEY ix_trading_agent_batches_fingerprint (config_fingerprint),
  CONSTRAINT fk_trading_agent_batches_config FOREIGN KEY (strategy_config_id) REFERENCES strategy_configs(id),
  CONSTRAINT fk_trading_agent_batches_account FOREIGN KEY (simulation_account_id) REFERENCES simulation_accounts(id),
  CONSTRAINT fk_trading_agent_batches_run FOREIGN KEY (rebalance_run_id) REFERENCES strategy_runs(id)
);

CREATE TABLE trading_agent_candidate_analyses (
  id INTEGER NOT NULL AUTO_INCREMENT,
  batch_id INTEGER NOT NULL,
  stock_id INTEGER NOT NULL,
  rank INTEGER NULL,
  is_holding BOOL NOT NULL DEFAULT FALSE,
  status VARCHAR(24) NOT NULL,
  rating VARCHAR(24) NULL,
  ai_target_weight FLOAT NULL,
  report_uri VARCHAR(512) NULL,
  report TEXT NULL,
  reasoning TEXT NULL,
  stats JSON NOT NULL,
  started_at DATETIME NULL,
  finished_at DATETIME NULL,
  error_message TEXT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_trading_agent_analysis_batch_stock (batch_id, stock_id),
  KEY ix_trading_agent_analyses_batch (batch_id),
  KEY ix_trading_agent_analyses_stock (stock_id),
  CONSTRAINT fk_trading_agent_analyses_batch FOREIGN KEY (batch_id) REFERENCES trading_agent_batches(id),
  CONSTRAINT fk_trading_agent_analyses_stock FOREIGN KEY (stock_id) REFERENCES stocks(id)
);

CREATE TABLE trading_agent_portfolio_decisions (
  id INTEGER NOT NULL AUTO_INCREMENT,
  batch_id INTEGER NOT NULL,
  status VARCHAR(24) NOT NULL,
  position_mapping VARCHAR(32) NOT NULL,
  target_weights JSON NOT NULL,
  rankings JSON NOT NULL,
  rationale TEXT NOT NULL,
  model VARCHAR(64) NOT NULL,
  llm_calls INTEGER NOT NULL DEFAULT 0,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_trading_agent_decision_batch (batch_id),
  CONSTRAINT fk_trading_agent_decision_batch FOREIGN KEY (batch_id) REFERENCES trading_agent_batches(id)
);
