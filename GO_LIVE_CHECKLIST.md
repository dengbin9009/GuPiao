# GuPiao 上线检查清单

## 基础与安全

- [ ] 目标代码版本、部署时间和回滚窗口已确认。
- [ ] 数据库和 `data/` 已备份并验证可读。
- [ ] 管理员密码和会话密钥不是开发默认值。
- [ ] IP 白名单只包含预期网络。
- [ ] `LIVE_TRADING_ENABLED=false`。
- [ ] `BROKER_ADAPTER=simulation`。
- [ ] 真实盘账户全部只读或停用，真实订单数为 0。

## 构建与测试

- [ ] Python 3.12 与 Node.js 20 已确认。
- [ ] `backend/.venv/bin/ruff check app tests` 通过。
- [ ] `backend/.venv/bin/pytest -q` 通过。
- [ ] `frontend` 的 `npm run build` 通过。
- [ ] `docker compose config --quiet` 通过。
- [ ] MySQL 旧库已执行 `0002_tradingagents_auto.sql`。

## 五进程

- [ ] backend 健康。
- [ ] 通用 Worker 存活。
- [ ] scheduler 存活。
- [ ] tradingagents-worker 存活。
- [ ] frontend 可访问。
- [ ] 五个应用进程连续运行至少 60 秒。

## 模拟盘回归

- [ ] 一夜持股法仍使用原模拟账户和原计划。
- [ ] 一夜持股法手动运行、计划运行和退出链路正常。
- [ ] TradingAgents 使用独立 10 万元模拟账户。
- [ ] 两个账户的现金、持仓、订单和收益完全隔离。
- [ ] 账户总资产等于现金加全部持仓市值。

## TradingAgents 数据与分析

- [ ] `OPENAI_API_KEY` 仅存在于运行环境，不出现在 API、日志、数据库和报告。
- [ ] 固定依赖版本为 `v0.3.1` / `01477f9`。
- [ ] 13:25 全市场实时行情固化成功。
- [ ] Top 100 加持仓具备至少 60 根已完成日线。
- [ ] 公告源健康且未过期。
- [ ] Top 10 排名与快照 SHA-256 已保存。
- [ ] 任一候选失败时组合决策和订单均为 0。
- [ ] 预算触顶、错过 14:42 或 Worker 中断时不交易。

## 无下单演练

- [ ] `dry_run=true`。
- [ ] 完成一次 Top 10 加持仓的真实 API 全批次分析。
- [ ] 批次状态为 `ready` 后执行演练。
- [ ] 演练状态为 `dry_run_completed`。
- [ ] 演练订单、成交和资金流水增量均为 0。
- [ ] readiness 显示演练已通过。

## 自动模拟交易

- [ ] `dry_run=false` 已人工保存。
- [ ] 单笔限额至少支持目标 20% 且不超过批准值。
- [ ] 单股 20%、总仓位 60%、最多 5 只已核对。
- [ ] T+1、100 股整数手、佣金、印花税和滑点已验证。
- [ ] 先启用 `agent_analysis`。
- [ ] 再启用 `agent_rebalance`。
- [ ] 14:45 至 14:50 的重试与幂等已验证。

## 回滚触发条件

- [ ] 任一应用进程反复退出。
- [ ] 登录或策略中心无法使用。
- [ ] 快照来源、时间或哈希不可审计。
- [ ] 出现跨模拟账户串账。
- [ ] 全批失败后仍产生订单。
- [ ] 出现任何 TradingAgents 真实盘资格或真实订单。

命中任一回滚条件时，立即停用两条 AI 计划，停止 tradingagents-worker，恢复代码和数据库备份，并重新确认真实盘关闭。
