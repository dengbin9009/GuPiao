# 快速开始：TradingAgents 自动模拟策略

## 1. 安装依赖

```bash
cd backend
uv pip install -e '.[market,agents,dev]'
```

`agents` 依赖固定到 TradingAgents `v0.3.1` 的提交 `01477f9`。

## 2. 配置环境

在项目既有环境配置中增加 `OPENAI_API_KEY`。使用 OpenAI 兼容服务时，同时增加
`OPENAI_BASE_URL`（通常填写到 `/v1`，不要包含 `/chat/completions`），并确认：

```text
LIVE_TRADING_ENABLED=false
BROKER_ADAPTER=simulation
SIMULATION_MAX_ORDER_NOTIONAL_ABS=20000
```

系统不会通过 API 返回密钥或接口地址，只会报告是否已配置。快速模型和深度模型
名称在策略中心配置，必须使用兼容服务实际提供的模型标识。

`TRADING_AGENTS_DATA_ROOT` 可指定快照和上游报告目录；Docker Compose 固定为 `/data/trading-agents` 并挂载持久卷。本地默认使用项目 `data/trading-agents`。

## 3. 启动五个进程

本地观察：

```bash
./start_tonight_observe.sh
```

Docker：

```bash
docker compose up --build
```

进程包括后端、通用 Worker、调度器、TradingAgents Worker 和前端。

## 4. 完成无下单演练

1. 打开“策略中心”。
2. 确认就绪状态中的密钥、固定依赖和模拟盘隔离均通过。
3. 保持“无下单演练”勾选，保存配置。
4. 在 13:25 后完成行情固化，创建分析批次。
5. 批次变为 `ready` 后打开详情，执行演练。
6. 确认批次变为 `dry_run_completed` 且订单数为 0。

## 5. 启用自动模拟交易

1. 取消“无下单演练”并保存。
2. 先启用 `agent_analysis`，再启用 `agent_rebalance`。
3. 后端会再次校验密钥、固定依赖、模拟盘隔离和成功演练；不满足时拒绝启用。

## 失败行为

- 任何候选失败、超时或预算超限：整批不调仓。
- 快照缺失或过期：不创建批次。
- 错过 14:42：分析失败。
- 14:50 后仍未满足调仓条件：当天不交易。
- T+1、现金或仓位预检失败：订单数为 0，原持仓保持不变。
