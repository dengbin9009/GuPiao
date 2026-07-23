-- GuPiao 一夜持股概率组合增量迁移（MySQL 8.4）
-- 执行前必须备份数据库；本迁移不会删除已有表或数据。

SET NAMES utf8mb4;

ALTER TABLE stocks
  ADD COLUMN listing_date VARCHAR(10) NULL,
  ADD COLUMN float_shares FLOAT NULL,
  ADD COLUMN turnover_rate FLOAT NULL,
  ADD COLUMN open_price FLOAT NULL,
  ADD COLUMN high_price FLOAT NULL,
  ADD COLUMN low_price FLOAT NULL,
  ADD COLUMN volume FLOAT NULL,
  ADD COLUMN vwap FLOAT NULL,
  ADD COLUMN tail_30m_return FLOAT NULL,
  ADD COLUMN limit_up_price FLOAT NULL,
  ADD COLUMN limit_down_price FLOAT NULL,
  ADD COLUMN quote_source VARCHAR(32) NULL,
  ADD COLUMN factor_updated_at DATETIME NULL;

CREATE TABLE probability_model_artifacts (
  id INTEGER NOT NULL AUTO_INCREMENT,
  model_version VARCHAR(64) NOT NULL,
  feature_version VARCHAR(32) NOT NULL,
  status VARCHAR(24) NOT NULL,
  trained_through VARCHAR(10) NOT NULL,
  training_sample_count INTEGER NOT NULL DEFAULT 0,
  calibration_sample_count INTEGER NOT NULL DEFAULT 0,
  calibration_start VARCHAR(10) NULL,
  calibration_end VARCHAR(10) NULL,
  brier_score FLOAT NULL,
  coefficients JSON NOT NULL,
  calibration_curve JSON NOT NULL,
  artifact_sha256 VARCHAR(64) NULL,
  error_message TEXT NULL,
  created_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_probability_model_version (model_version),
  KEY ix_probability_model_feature_version (feature_version),
  KEY ix_probability_model_trained_through (trained_through),
  KEY ix_probability_model_sha256 (artifact_sha256)
);

CREATE TABLE probability_training_samples (
  id INTEGER NOT NULL AUTO_INCREMENT,
  stock_id INTEGER NOT NULL,
  entry_at DATETIME NOT NULL,
  exit_at DATETIME NOT NULL,
  feature_version VARCHAR(32) NOT NULL,
  features JSON NOT NULL,
  net_return FLOAT NOT NULL,
  profitable BOOL NOT NULL,
  source_sha256 VARCHAR(64) NOT NULL,
  created_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_probability_sample (stock_id, entry_at, feature_version),
  KEY ix_probability_sample_stock (stock_id),
  KEY ix_probability_sample_entry (entry_at),
  KEY ix_probability_sample_feature (feature_version),
  CONSTRAINT fk_probability_sample_stock FOREIGN KEY (stock_id) REFERENCES stocks(id)
);

CREATE TABLE probability_portfolio_runs (
  id INTEGER NOT NULL AUTO_INCREMENT,
  strategy_run_id INTEGER NULL,
  strategy_config_id INTEGER NOT NULL,
  simulation_account_id INTEGER NOT NULL,
  trading_date VARCHAR(10) NOT NULL,
  trigger_type VARCHAR(32) NOT NULL,
  status VARCHAR(24) NOT NULL,
  dry_run BOOL NOT NULL DEFAULT TRUE,
  model_artifact_id INTEGER NULL,
  snapshot_sha256 VARCHAR(64) NULL,
  config_fingerprint VARCHAR(64) NULL,
  selected_count INTEGER NOT NULL DEFAULT 0,
  order_ids JSON NOT NULL,
  error_message TEXT NULL,
  created_at DATETIME NOT NULL,
  completed_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_probability_run_config_date_trigger (
    strategy_config_id, trading_date, trigger_type
  ),
  UNIQUE KEY uq_probability_run_strategy_run (strategy_run_id),
  KEY ix_probability_run_account (simulation_account_id),
  KEY ix_probability_run_date (trading_date),
  KEY ix_probability_run_fingerprint (config_fingerprint),
  CONSTRAINT fk_probability_run_strategy_run FOREIGN KEY (strategy_run_id) REFERENCES strategy_runs(id),
  CONSTRAINT fk_probability_run_config FOREIGN KEY (strategy_config_id) REFERENCES strategy_configs(id),
  CONSTRAINT fk_probability_run_account FOREIGN KEY (simulation_account_id) REFERENCES simulation_accounts(id),
  CONSTRAINT fk_probability_run_model FOREIGN KEY (model_artifact_id) REFERENCES probability_model_artifacts(id)
);

CREATE TABLE probability_candidate_decisions (
  id INTEGER NOT NULL AUTO_INCREMENT,
  portfolio_run_id INTEGER NOT NULL,
  stock_id INTEGER NOT NULL,
  status VARCHAR(24) NOT NULL,
  `rank` INTEGER NULL,
  features JSON NOT NULL,
  rejection_reasons JSON NOT NULL,
  raw_probability FLOAT NULL,
  calibrated_probability FLOAT NULL,
  expected_net_return FLOAT NULL,
  volatility_20d FLOAT NULL,
  score FLOAT NULL,
  target_weight FLOAT NULL,
  target_notional FLOAT NULL,
  planned_quantity INTEGER NULL,
  order_id INTEGER NULL,
  created_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_probability_decision_run_stock (portfolio_run_id, stock_id),
  KEY ix_probability_decision_stock (stock_id),
  CONSTRAINT fk_probability_decision_run FOREIGN KEY (portfolio_run_id) REFERENCES probability_portfolio_runs(id),
  CONSTRAINT fk_probability_decision_stock FOREIGN KEY (stock_id) REFERENCES stocks(id),
  CONSTRAINT fk_probability_decision_order FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE strategy_position_lots (
  id INTEGER NOT NULL AUTO_INCREMENT,
  strategy_config_id INTEGER NOT NULL,
  account_id INTEGER NOT NULL,
  stock_id INTEGER NOT NULL,
  buy_order_id INTEGER NOT NULL,
  buy_fill_id INTEGER NOT NULL,
  original_quantity INTEGER NOT NULL,
  remaining_quantity INTEGER NOT NULL,
  available_on VARCHAR(10) NOT NULL,
  planned_exit_at DATETIME NOT NULL,
  status VARCHAR(24) NOT NULL,
  last_exit_attempt_at DATETIME NULL,
  close_order_ids JSON NOT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_strategy_position_lot_buy_order (buy_order_id),
  UNIQUE KEY uq_strategy_position_lot_buy_fill (buy_fill_id),
  KEY ix_strategy_position_lot_config (strategy_config_id),
  KEY ix_strategy_position_lot_account (account_id),
  KEY ix_strategy_position_lot_stock (stock_id),
  KEY ix_strategy_position_lot_available_on (available_on),
  KEY ix_strategy_position_lot_planned_exit (planned_exit_at),
  CONSTRAINT fk_strategy_position_lot_config FOREIGN KEY (strategy_config_id) REFERENCES strategy_configs(id),
  CONSTRAINT fk_strategy_position_lot_stock FOREIGN KEY (stock_id) REFERENCES stocks(id),
  CONSTRAINT fk_strategy_position_lot_order FOREIGN KEY (buy_order_id) REFERENCES orders(id),
  CONSTRAINT fk_strategy_position_lot_fill FOREIGN KEY (buy_fill_id) REFERENCES fills(id)
);
