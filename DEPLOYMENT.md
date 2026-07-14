# GuPiao 部署手册

## 支持范围

GuPiao 支持 Mac 本地开发和 Linux Docker Compose 部署。默认且持续保持模拟盘；真实盘只有在券商适配器、账户、权限和风控全部人工验收后才允许进入单独上线流程。

应用由五个进程组成：

- `backend`：FastAPI API
- `worker`：行情、公告和通知 Worker
- `scheduler`：交易日计划调度器
- `tradingagents-worker`：TradingAgents 独立分析 Worker
- `frontend`：Vue/Nginx 控制台

## 环境要求

- Python 3.12
- Node.js 20
- Docker Engine 与 Docker Compose（Linux 部署）
- MySQL 8.4（Compose 已包含）
- 出站访问 PyPI、npm 和 GitHub

## 环境配置

```bash
cp .env.example .env
```

模拟盘最小安全边界：

```dotenv
LIVE_TRADING_ENABLED=false
BROKER_ADAPTER=simulation
SIMULATION_MAX_ORDER_NOTIONAL_ABS=20000
```

TradingAgents 还需要 `OPENAI_API_KEY`。API 只返回是否配置，不返回密钥值。

## 本地启动

安装后端依赖：

```bash
cd backend
uv pip install -e '.[market,agents,dev]'
```

一键启动五进程：

```bash
./start_tonight_observe.sh
```

访问：

- 控制台：http://127.0.0.1:5173
- 健康检查：http://127.0.0.1:8000/api/health

日志包括：`.uvicorn.log`、`.worker.log`、`.scheduler.log`、`.tradingagents-worker.log` 和 `.vite.log`。

Mac LaunchAgent 模板位于 `deploy/launchagents/com.gupiao.tradingagents-worker.plist.example`，使用前必须把占位路径替换为绝对路径，并通过 LaunchAgent 自身的安全环境注入密钥。

## Linux Docker Compose

```bash
docker compose up --build -d
docker compose ps
```

前端：http://服务器地址:8080
后端健康：http://服务器地址:8000/api/health

Compose 为五个应用进程和 MySQL 都配置了健康检查。`tradingagents-worker` 和 API 镜像安装固定提交的可选依赖。

## 数据库升级

升级前先备份：

```bash
docker compose exec mysql mysqldump -ugupiao -p gupiao > gupiao-before-upgrade.sql
```

现有 MySQL 执行：

```bash
docker compose exec -T mysql mysql -ugupiao -p gupiao < backend/migrations/0002_tradingagents_auto.sql
```

空数据库由 SQLAlchemy 启动建表；SQLite 旧库由启动时迁移补列。

## TradingAgents 启用顺序

1. 保持两条 AI 计划停用，`dry_run=true`。
2. 确认密钥、固定依赖、模拟盘隔离均通过 readiness。
3. 13:25 后固化行情和公告，创建 Top 10 分析批次。
4. 批次 `ready` 后执行无下单演练，确认状态为 `dry_run_completed` 且订单数为 0。
5. 把 `dry_run` 改为 `false`。
6. 先启用 `agent_analysis`，再启用 `agent_rebalance`。

任一门禁不满足时，后端拒绝启用自动计划。

## 备份目录

- 数据库
- `data/market/`
- `data/backtests/`
- `data/plugins/`
- `data/trading-agents/`
- `.env`（按密钥文件标准保护）

## 故障处理

- readiness 显示依赖未安装：重建带 `agents` 依赖的后端镜像或重新安装可选依赖。
- 批次无法创建：检查 13:25 实时行情、60 根已完成日线和公告源时效。
- 批次失败：查看单股错误、预算、14:42 截止时间和子进程日志。
- 调仓被拦截：检查 T+1、现金、单笔 2 万、单股 20%、总仓位 60% 和紧急停止。
- 前端可开但 API 失败：检查 backend 健康、代理目标和登录会话。

## 回滚

1. 停止服务。
2. 恢复上一版本代码和数据库备份。
3. 恢复数据目录。
4. 重新启动并检查五个应用进程。
5. 确认真实盘仍为关闭状态、真实订单数为 0。
