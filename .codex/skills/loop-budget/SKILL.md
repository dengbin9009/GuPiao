---
name: loop-budget
description: 用于 GuPiao Loop 每轮开始和结束时检查运行、token、迭代与同错重试预算。
---

# GuPiao Loop 预算守卫

每轮开始和结束都执行。

## 开始时

1. 读取 `loop-budget.md`、`loop-run-log.md`、`STATE.md` 和 `loop-constraints.md`。
2. 汇总最近 24 小时运行次数与 token，并读取当前迭代号和同一错误尝试次数。
3. 第一周强制 L1 `report-only`，子代理上限为 0。
4. 达到预算 80% 时只报告；达到 100%、迭代超过 6 次或 `loop-pause-all` 生效时立即退出。
5. 同一错误第 3 次仍失败时熔断并升级人工。
6. 没有可执行事项时在 5k token 内结束。

## 结束时

按 `loop-run-log.md` 的 JSON 格式追加记录，包含迭代号、同错尝试次数、worktree、安全模式、结果和证据摘要。不得记录 `.env` 内容或任何敏感信息。

## 强制规则

- 不得超过 `loop-budget.md` 中的子代理、运行或 token 上限。
- 不得用新循环、重命名错误或拆分错误规避熔断。
- 超限时更新 `STATE.md`，结果只能是 `report-only`、`no-op` 或 `escalated`。
- 不创建或修改 Codex Automation。
