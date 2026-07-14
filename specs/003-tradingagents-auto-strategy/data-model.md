# 数据模型：TradingAgents 自动策略

## 新增实体

### MarketDailyBar

股票已完成日线，按股票和交易日唯一。保存开高低收、成交量、成交额、来源和抓取时间。

### TradingAgentBatch

每日分析批次，保存策略配置、独立账户、档位、模型、提示词版本、候选、持仓、核心行情、公告、冻结补充数据、快照哈希、配置指纹、预算消耗、租约、截止时间、组合订单和错误。配置指纹用于证明无下单演练与待启用自动计划采用同一套策略参数和模拟账户；`dry_run` 本身不参与指纹计算。

主要状态：`pending`、`processing`、`ready`、`failed`、`cancelled`、`blocked`、`dry_run_completed`、`rebalanced`。

### TradingAgentCandidateAnalysis

每个批次和股票唯一。保存预筛排名、是否持仓、五级评级、AI 目标仓位、完整报告、理由、Token、开始/结束时间和错误。

### TradingAgentPortfolioDecision

每批次唯一。保存仓位映射、跨股票排名、目标权重、理由、模型和预算消耗。

## 修改实体

### StrategyConfig

新增可空 `simulation_account_id`。TradingAgents 配置必须绑定未被其他策略配置占用的活跃模拟账户，且 `mode` 固定为 `SIMULATION`。

## 账户关系

“TradingAgents 模拟账户”拥有独立的订单、成交、持仓、资金流水和账户快照。账户总资产统一为：

```text
总资产 = 现金余额 + 所有当前持仓市值
```
