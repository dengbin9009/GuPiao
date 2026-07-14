# GuPiao Loop 运行日志

每次人工启动循环追加一条记录。日志只保存命令结果摘要，不得包含 `.env` 内容、密钥、令牌、密码、账户信息或真实订单信息。第一周的 `outcome` 只能是 `report-only`、`no-op` 或 `escalated`。

## 记录格式

```json
{
  "run_id": "ISO-8601 时间",
  "pattern": "daily-triage",
  "mode": "L1-report-only",
  "iteration": 1,
  "duration_s": 45,
  "items_found": 4,
  "actions_taken": 0,
  "same_error_attempt": 0,
  "escalations": 0,
  "tokens_estimate": 12000,
  "worktree": "专用 worktree 路径",
  "safety": "LIVE=false; BROKER=simulation",
  "outcome": "report-only | no-op | escalated",
  "evidence": ["不含敏感信息的命令与结果摘要"]
}
```

```json
{
  "run_id": "2026-07-14-tradingagents-auto-implementation",
  "pattern": "daily-triage",
  "mode": "manual-approved-fix",
  "iteration": 3,
  "items_found": 18,
  "actions_taken": 18,
  "same_error_attempt": 0,
  "escalations": 0,
  "worktree": ".worktrees/restore-simulation-loop",
  "safety": "SIMULATION only; dry_run=true; AI schedules disabled; 真实订单数为 0",
  "outcome": "done-with-external-readiness-gates",
  "evidence": [
    "Python 3.12.12 后端全套 176 passed，Ruff 全部通过，wheel 构建成功",
    "Node 20 前端生产构建通过，共转换 1560 个模块",
    "TradingAgents 固定依赖版本 0.3.1，提交 01477f9，Apache 2.0 说明已补充",
    "LaunchAgent 托管的五个本地进程连续运行超过 5 分钟，后端健康端点与前端入口均返回 HTTP 200",
    "TradingAgents 独立模拟账户初始资金 100000，两条 AI 自动计划关闭，真实订单和启用实盘账户均为 0",
    "Docker Compose 配置通过；镜像构建被本机 dockerproxy.com 证书错误阻塞，未修改用户 Docker Desktop 配置",
    "无 OpenAI 密钥时 readiness 失败关闭，自动计划无法启用，真实 API 演练等待管理员配置密钥",
    "Yahoo 补充基本面和新闻在 LLM 调用前冻结进 SHA-256 快照，分析子进程不临场访问外部数据工具"
  ]
}
```

## 记录规则

- 第一周不得记录自动代码修复；`actions_taken` 应为 0。
- 每轮记录当前迭代号；超过 6 次前必须停止。
- 同一错误按稳定错误签名累计，达到 3 次时记录 `escalated` 并熔断。
- 代码修复必须记录非 `main` 的专用 worktree 路径。
- push、merge、deploy、真实交易均不得作为循环动作记录；若被请求，记录为人工门升级。
- 保留最近 30 天，清理前由人工确认。

## 最近运行

<!-- 循环在此行下方追加 JSON；当前尚无正式运行记录。 -->

```json
{
  "run_id": "2026-07-10-simulation-auto-runtime",
  "pattern": "daily-triage",
  "mode": "manual-approved-fix",
  "iteration": 2,
  "items_found": 20,
  "actions_taken": 20,
  "same_error_attempt": 0,
  "escalations": 0,
  "worktree": ".worktrees/restore-simulation-loop",
  "safety": "LIVE=false; BROKER=simulation; 真实订单数为 0",
  "outcome": "done-with-scheduled-observation",
  "evidence": [
    "Ruff、后端全套 103 passed、6 个验收脚本和前端 Node 20 构建通过",
    "AKShare 股票主数据 5530 只，mootdx 行情 5203 只，公告记录 9059 条",
    "scheduler 原子占用、租约恢复和端到端测试证明 14:40 到点创建 1 笔模拟订单且同窗口不重复",
    "独立审查发现的 LIVE 硬门、旧库迁移、调度并发、退出重试、事件风险、锁恢复、公告周期和持仓归属问题均已修复并覆盖测试",
    "LaunchAgent 四进程连续超过 90 秒；backend 与 frontend HTTP 健康，caffeinate 防睡眠有效",
    "认证后总览、策略、计划、模拟账户、订单、持仓、数据源接口均返回 200",
    "LIVE 环境与数据库双关闭，启用 LIVE API 返回 403，启用账户/计划/真实订单均为 0",
    "当前为周六非交易日；下一合法入场窗口为 2026-07-13 14:40"
  ]
}
```

```json
{
  "run_id": "2026-07-10-simulation-recovery",
  "pattern": "daily-triage",
  "mode": "L1-report-only",
  "iteration": 1,
  "items_found": 4,
  "actions_taken": 0,
  "same_error_attempt": 1,
  "escalations": 0,
  "worktree": ".worktrees/restore-simulation-loop",
  "safety": "LIVE=false; BROKER=simulation; 未发送真实订单",
  "outcome": "report-only",
  "evidence": [
    "复核人工批准的受限修复：四进程与两个 HTTP 端点连续 60 秒通过",
    "复核人工批准的受限修复：60m 最近隔夜回测退出码 0，收益率 -0.2419%",
    "复核人工批准的受限修复：后端全套测试 71 passed，6 个验收脚本通过",
    "复核人工批准的受限修复：Node 20 前端构建通过",
    "Loop 配置审计 100/100；LIVE 关闭且真实订单数为 0"
  ]
}
```
