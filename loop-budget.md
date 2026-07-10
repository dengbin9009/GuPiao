# GuPiao Loop 预算

> 主循环：**Daily Triage**。第一周固定为 L1 `report-only`。

## 每日上限

| 循环 | 每日最多运行 | 每日最多 token | 每轮最多子代理 | 允许动作 |
|---|---:|---:|---:|---|
| Daily Triage（第一周） | 1 | 50k | 0 | 只报告 |
| 人工批准的修复轮 | 2 | 100k | 2 | 仅专用 worktree 内的最小修复 |

## 单任务上限

- 最多 6 次迭代。
- 同一错误最多 3 次尝试，随后熔断并升级人工。
- 达到每日预算 80% 时立即切回 `report-only`，不得启动新修复。
- 达到每日预算 100% 或发现 `loop-pause-all` 时立即退出。
- 没有可执行事项时在 5k token 内结束，不生成额外工作。

## 超限处理

1. 停止当前循环，不创建或修改任何 Automation。
2. 向 `loop-run-log.md` 追加超限或熔断记录。
3. 在 `STATE.md` 高优先级区写明阻塞原因和所需人工动作。
4. 不得用新的循环、子代理或变更错误描述规避上限。

## 总开关

- 开关：`loop-pause-all`。
- 仅人工可清除；恢复前重新确认实盘和密钥硬门禁。

## 成本估算

```bash
PATH=/Users/dengbin/.nvm/versions/node/v20.19.4/bin:/usr/bin:/bin:/usr/sbin:/sbin npx @cobusgreyling/loop-cost --pattern daily-triage --level L1
```

## 本周期告警

- 无。
