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
