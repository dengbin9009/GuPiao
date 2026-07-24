# GuPiao Codex Loop 配置

## 目标与模式

- 主模式：`daily-triage`。
- 当前目标：交付并验证八套策略独立模拟运营平台。
- 第一周级别：L1 `report-only`，只采集证据、更新 `STATE.md` 和 `loop-run-log.md`，不自动修改代码。
- 本项目不创建 Codex Automation；循环由人工显式启动。
- 代码修复仅能在专用 worktree 中进行，禁止在 `main` 分支直接修改。

## 每轮流程

1. 读取相关 `specs/`、`STATE.md`、本文件、`loop-constraints.md` 和预算/运行日志。
2. 确认工作目录不是 `main`，并确认 `LIVE_TRADING_ENABLED=false`、`BROKER_ADAPTER=simulation`；不得读取 `.env` 来完成确认。
3. 第一周仅报告。人工批准进入修复阶段后，按 TDD 一次处理一个失败。
4. 每次修改后运行最小相关测试；准备退出时执行完整二进制验收。
5. 更新 `STATE.md` 与 `loop-run-log.md`，列明证据、失败原因、尝试次数和人工门状态。

## 二进制退出条件

只有下列条件全部为真时，结果才是 `DONE`；任何一项为假或缺少证据，结果均为 `DONE_WITH_CONCERNS` 或继续下一次迭代：

| 条件 | 通过标准 |
|---|---|
| 后端健康 | 健康端点成功响应，进程无启动错误 |
| 前端可达 | 前端入口返回 HTTP 200 |
| 后台进程存活 | worker、scheduler、TradingAgents Worker 与量化策略 Worker 均持续存活 |
| scheduler 可降级 | 交易日历数据源失败时 scheduler 不崩溃、主循环继续存活 |
| 八策略专项测试 | 八套策略公式、数据、任务、执行、API 和闸门测试退出码为 0 |
| 后端测试 | 后端全套 pytest 退出码为 0，无失败 |
| 前端测试与构建 | Node 20 下 `npm test` 和 `npm run build` 退出码均为 0 |
| 安全模式 | `LIVE_TRADING_ENABLED=false` 且 `BROKER_ADAPTER=simulation` |

不得以“部分通过”“曾经通过”或人工目测替代本轮退出证据。

## 迭代与熔断

- 单次恢复任务最多 6 次迭代，达到上限立即停止并交给人工。
- 同一错误最多尝试 3 次；第三次仍失败即熔断，不得通过换措辞或轻微改命令重置计数。
- 熔断后在 `STATE.md` 和 `loop-run-log.md` 记录错误签名、三次证据与建议的人工动作。
- 任一安全模式检查失败、检测到实盘配置、真实订单风险或疑似密钥暴露时立即熔断并升级人工处理。

## 人工门

以下操作一律不由循环自动执行：

- push、merge、deploy、发布、标记 PR ready 或关闭问题。
- 启用 `LIVE_TRADING_ENABLED`，或把 `BROKER_ADAPTER` 改出 `simulation`。
- 配置/调用真实券商适配器、解锁账户、提交真实订单。
- 读取、提交或输出 `.env` 密钥。

涉及任何实盘事项必须先获得人工明确确认；即使人工确认，也应由人工执行最终实盘动作。

### 安全门与自动合并策略（Safety gates / auto-merge policy）

- 安全门以 `AGENTS.md`、`loop-constraints.md` 和本节为准；任何冲突都采用更保守规则。
- 禁止自动合并，循环没有 auto-merge 权限。
- 人工升级路径（human-escalation path）：停止动作，在 `STATE.md` 标记阻塞并在 `loop-run-log.md` 记录非敏感证据，然后由仓库维护者决定后续处理。

## 停滞与无进展检测

- 每轮比较错误签名、测试结果和退出条件；若没有新增证据或失败状态没有变化，记为一次 no-progress/stall。
- 连续 2 轮无进展时切换为只报告并请求人工复核；同一错误第 3 次失败时按熔断规则升级人工，不再重试。

## MCP 与最小权限工具范围（least-privilege tool scope）

- 当前 `daily-triage` 不需要 MCP（MCP not required），也不配置 connector。
- triage 角色仅可读取规格、git 状态和测试/构建输出，并写入本任务允许的 Loop 文档。
- verifier 角色只核验和报告，不编辑代码、不执行外部写操作。
- 循环没有 issue、PR、push、merge、deploy、broker 或真实交易写权限。

## 预算

- 预算与节流规则见 `loop-budget.md`。
- 第一周每轮子代理数为 0，只允许 `report-only`。
- `loop-pause-all` 为总开关；出现时立即退出。
