# GuPiao Loop 状态

最后更新：2026-07-10

## 当前目标

恢复模拟盘最小可用链路。在不接触实盘、不读取密钥、不改变 `LIVE_TRADING_ENABLED=false` 与 `BROKER_ADAPTER=simulation` 的前提下，恢复后端、前端、worker、scheduler 和 60m 回测的可重复运行证据。

## 本轮结果

- 初始基线：后端 `58 passed / 3 failed`，scheduler 因交易日历提供方全部失败而退出。
- 当前后端：Python 3.12.12 下全套测试 `71 passed`；Ruff 与 6 个关键验收脚本全部通过。
- 当前前端：Node 20 下生产构建通过，共转换 1560 个模块。
- scheduler：模拟盘在交易日历提供方全部失败时按工作日降级，LIVE 配置仍 fail-closed；mootdx 不再声明不支持的交易日历能力。
- 一键启动：支持任意 clone 路径，幂等准备 10 万模拟账户、观察股票、一夜持股策略和两个启用计划；拒绝 LIVE 配置和非 simulation broker；启动失败会清理本轮进程。
- 数据真实性：启动时清空种子报价并把数据源置为待实际探测；行情超过 15 秒、公司事件超过 1800 秒或来源不健康时均禁止下单。
- 60m 回测：`000001.SZ`，2026-06-24 至 2026-06-25，`timeframe_used=60m`，`data_source=cache`，尾盘最后一根 K 线入场，净收益 `-24.1861`，收益率 `-0.2419%`，退出码 0。
- 安全状态：数据库 LIVE 关闭，真实订单数为 0，运行配置保持 `LIVE_TRADING_ENABLED=false`、`BROKER_ADAPTER=simulation`。
- 当前限制：本轮外部实时行情与公司事件源未通过实际探测；服务链路正常，但自动下单按设计保持失败关闭。

## 高优先级

1. 保持模拟盘服务运行，观察定时策略运行结果与数据源稳定性。
2. 外部行情不可用时记录错误，不得用演示数据冒充真实回测结果。
3. 在任何实盘工作开始前重新执行 fail-closed 风控审查并取得人工确认。

## 待验收清单

- [x] 后端健康检查成功。
- [x] 前端入口返回 HTTP 200。
- [x] worker 与 scheduler 均至少连续存活 60 秒。
- [x] 交易日历数据源失败后，模拟盘 scheduler 仍存活；LIVE 配置 fail-closed。
- [x] 使用 `--preferred-timeframe 60m` 的回测成功并保留结果证据。
- [x] 后端全套测试 `71 passed`，不再有 3 个失败。
- [x] 前端 Node 20 构建通过。
- [x] 运行时验证 `LIVE_TRADING_ENABLED=false`。
- [x] 运行时验证 `BROKER_ADAPTER=simulation`。
- [x] 所有修改均位于专用 worktree，未 deploy、merge 或发送真实订单。

## 观察项

- 外部行情与交易日历提供方可能不稳定；验收必须覆盖失败降级，不能只验证成功路径。
- 60m 回测结果为单次历史结果，不构成收益承诺。
- 外部行情或公司事件数据恢复前，策略不会创建模拟订单；这属于数据风控失败关闭，不是进程故障。

## 已知环境事项

- 原 Conda Python 3.12.3 的 `readline` 扩展会段错误；本轮改用 uv Python 3.12.12 验证。
- 系统 Homebrew Node 16 缺少 ICU；启动脚本优先选择本机 NVM Node 20。
- 第一周 Loop 仍为 report-only；未创建 Automation。

---

运行记录：见 `loop-run-log.md`。
