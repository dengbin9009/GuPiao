# 数据模型：一夜持股概率组合策略

## ProbabilityModelArtifact

概率模型产物，追加写入。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigint | 主键 |
| model_version | varchar(64) | 唯一模型版本 |
| feature_version | varchar(32) | 特征契约版本 |
| status | varchar(24) | `training/ready/rejected` |
| trained_through | date | 训练数据截止日期 |
| training_sample_count | int | 训练样本数 |
| calibration_sample_count | int | 校准样本数 |
| calibration_start/end | date | 校准时间范围 |
| brier_score | float | 校准集 Brier 分数 |
| coefficients | json | 标准化参数、逻辑回归系数和截距 |
| calibration_curve | json | 单调概率映射点 |
| artifact_sha256 | char(64) | 产物哈希 |
| error_message | text | 拒绝原因 |
| created_at | datetime | 创建时间 |

## ProbabilityTrainingSample

严格按交易窗口生成的历史训练样本，按股票和入场时间唯一。

| 字段 | 类型 | 说明 |
|---|---|---|
| stock_id | bigint | 股票 |
| entry_at | datetime | 14:40 入场时间 |
| exit_at | datetime | 下一交易日 10:30 退出时间 |
| feature_version | varchar(32) | 特征版本 |
| features | json | 截至入场时可得的特征 |
| net_return | float | 扣费后净收益率 |
| profitable | bool | 净收益是否大于 0 |
| source_sha256 | char(64) | 源窗口哈希 |

## ProbabilityPortfolioRun

一次入场或退出组合运行，按配置、交易日和触发类型唯一。

| 字段 | 类型 | 说明 |
|---|---|---|
| strategy_run_id | bigint | 对应通用策略运行 |
| strategy_config_id | bigint | 策略配置 |
| simulation_account_id | bigint | 独立模拟账户 |
| trading_date | date | 交易日 |
| trigger_type | varchar(32) | `portfolio_entry/portfolio_exit` |
| status | varchar(24) | `running/completed/blocked` |
| dry_run | bool | 是否无下单演练 |
| model_artifact_id | bigint? | 使用的模型产物 |
| snapshot_sha256 | char(64)? | 候选快照哈希 |
| config_fingerprint | char(64)? | 除演练开关外的配置与账户指纹 |
| selected_count | int | 最终选择数 |
| order_ids | json | 创建的模拟订单 |
| error_message | text? | 阻断原因 |

## ProbabilityCandidateDecision

一次组合运行中的逐股决策，按运行和股票唯一。

| 字段 | 类型 | 说明 |
|---|---|---|
| portfolio_run_id | bigint | 组合运行 |
| stock_id | bigint | 股票 |
| status | varchar(24) | `rejected/selected/filled/skipped` |
| rank | int? | 排名 |
| features | json | 决策因子 |
| rejection_reasons | json | 拒绝或跳过原因 |
| raw_probability | float? | 原始概率 |
| calibrated_probability | float? | 校准概率 |
| expected_net_return | float? | 预期净收益率 |
| volatility_20d | float? | 20 日波动率 |
| score | float? | 风险调整分数 |
| target_weight | float? | 目标仓位 |
| target_notional | float? | 目标金额 |
| planned_quantity | int? | 计划股数 |
| order_id | bigint? | 成交订单 |

## StrategyPositionLot

策略持仓归属批次。

| 字段 | 类型 | 说明 |
|---|---|---|
| strategy_config_id | bigint | 所属策略配置 |
| account_id | bigint | 模拟账户 |
| stock_id | bigint | 股票 |
| buy_order_id/fill_id | bigint | 买入审计记录 |
| original_quantity | int | 原始数量 |
| remaining_quantity | int | 剩余数量 |
| available_on | date | T+1 可卖日期 |
| planned_exit_at | datetime | 计划退出时间 |
| status | varchar(24) | `open/closed/blocked` |
| last_exit_attempt_at | datetime? | 最近退出尝试 |
| close_order_ids | json | 卖出订单列表 |

## 既有实体调整

- `Stock` 增加真实上市日期、流通股本、换手率、当日开高低、VWAP、涨跌停状态和因子更新时间字段。流通股本用于按最新累计成交量重算换手率。
- `MarketDailyBar` 继续保存日线，用于 MA5、MA20、收益率、波动率和平均成交额。
- `SimulationAccount` 不新增策略专属字段；独立性由账户和策略配置绑定保证。
- `AccountSnapshot` 继续保存统一估值结果，并作为日亏损基准。
