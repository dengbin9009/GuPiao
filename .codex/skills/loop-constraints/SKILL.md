---
name: loop-constraints
description: 用于每次 GuPiao Loop 开始前加载并强制执行项目安全、分支、预算和人工门约束。
user_invocable: true
---

# GuPiao Loop 约束执行器

在任何检查或动作前必须：

1. 读取项目根目录 `loop-constraints.md`，逐条加载为强制规则。
2. 读取 `AGENTS.md`、`STATE.md` 和 `LOOP.md`，以更严格规则为准。
3. 检查 `loop-pause-all`；若生效立即退出。
4. 确认代码修复位于专用 worktree 且不在 `main`。
5. 不读取 `.env`；若任务需要读取或输出密钥，立即升级人工。

## 动作前检查

- **编辑前**：第一周只报告；非第一周也必须有人工批准、TDD 失败证据和最小作用域。
- **验证前**：显式保持 `LIVE_TRADING_ENABLED=false` 与 `BROKER_ADAPTER=simulation`，不得连接真实券商。
- **重试前**：检查同一错误次数；第 3 次失败后熔断。
- **退出前**：核对最多 6 次迭代和 `LOOP.md` 的全部二进制退出条件。
- **外部写操作前**：push、merge、deploy、发布、真实交易一律停止并请求人工确认，循环不得代为执行。

## 启动输出

```text
已从 loop-constraints.md 加载约束：第一周仅报告；worktree-only；LIVE=false；BROKER=simulation；实盘与外部写操作需人工门。
```

若约束文件缺失，按最保守策略立即停止并升级人工，不得继续执行默认修复。
