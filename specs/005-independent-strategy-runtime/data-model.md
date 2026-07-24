# 数据模型：八套独立策略平台

## 扩展实体

- `Stock`：新增 `instrument_type`、`lot_size`、`settlement_days`。
- `MarketDailyBar`：保留原始 OHLCV，新增复权收盘、复权因子和数据质量标识。

## 新实体

- `MarketDailyMetric`：交易日、市盈率、市净率、股息率、总市值和流通市值。
- `FinancialReportSnapshot`：报告期、公告日、实际公告日、可用日、ROE、毛利率、经营现金流、净利润、营业收入、总资产、总负债和来源。
- `QuantStrategyTask`：策略配置、任务类型、交易日、幂等键、状态、租约、尝试次数和错误。
- `QuantPortfolioDecision`：策略运行、账户、状态、数据截止、快照哈希、配置指纹、目标仓位和订单编号。
- `QuantCandidateScore`：证券、排名、特征、分数、目标仓位和拒绝原因。
- `StrategyRiskProfile`：单策略日亏损、最大回撤、连续错误、订单上限和紧急停止。
- `StrategyPerformanceDaily`：现金、市值、总资产、日收益、累计收益、回撤和暴露。
- `StrategyBacktestQualification`：回测区间、完整率、样本外指标、交易数、版本和是否合格。
- `StrategyDryRunApproval`：配置指纹、策略版本、数据版本、决策编号和验证时间。

所有决策、候选、任务结果、绩效和闸门记录均为追加写入，不删除历史。
