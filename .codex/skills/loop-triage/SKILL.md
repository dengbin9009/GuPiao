---
name: loop-triage
description: 用于 GuPiao 模拟盘恢复任务的每日检查、失败分类与状态更新。
user_invocable: true
---

# GuPiao Loop 检查

## 开始前

1. 读取相关 `specs/`、`STATE.md`、`LOOP.md`、`loop-constraints.md`、`loop-budget.md` 和最近运行日志。
2. 确认当前不是 `main` 分支，且工作位于专用 worktree。
3. 不读取 `.env`；只用无敏感信息的代码默认值、规格或显式安全参数确认 `LIVE=false`、`BROKER=simulation`。
4. 若发现 `loop-pause-all`、实盘风险、密钥风险或非 worktree 修改，立即升级人工。

## 第一周行为

- 固定为 L1 `report-only`。
- 不修改业务代码，不创建 Automation，不启动子代理，不 push、merge 或 deploy。
- 只采集现状证据，并更新允许的 Loop 文档。

## 输出

按以下顺序给出简洁中文报告：

1. **高优先级**：影响模拟盘最小链路的可复现问题、证据和下一步人工动作。
2. **观察项**：暂不行动的数据源波动、非阻塞风险和待复验证据。
3. **忽略项**：与当前目标无关的噪声。
4. **退出条件差距**：逐项列出 `LOOP.md` 二进制条件的通过/失败/缺证据状态。
5. **状态更新**：更新 `STATE.md` 和 `loop-run-log.md`，不得写入敏感信息。

## 规则

- 只报告可验证事实，不把历史成功当成本轮成功。
- 同一错误按稳定签名计数；第 3 次失败熔断。
- 单任务最多 6 次迭代。
- 实盘、推送、合并、部署都只能进入人工门，不能成为自动建议动作。
