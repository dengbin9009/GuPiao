# 快速开始：八套独立量化策略

## 安全前提

```text
LIVE_TRADING_ENABLED=false
BROKER_ADAPTER=simulation
```

系统首次启动会幂等建立八个 200 万元模拟账户和十六条默认关闭计划，不会重置已有账户。

## 本地启动

```bash
cd backend
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[market,agents,dev]'

cd ../frontend
PATH=/Users/dengbin/.nvm/versions/node/v20.19.4/bin:/usr/bin:/bin:/usr/sbin:/sbin npm install
```

启动脚本会运行 backend、frontend、worker、scheduler、TradingAgents Worker 和 `quant-strategy-worker`。
API 后端是唯一数据库初始化者；本地脚本和 Docker Compose 都会等待后端健康后再启动后台进程，
避免并发种子化产生重复策略或账户。

SQLite 开发库会在启动时自动执行幂等运行时迁移。首次启动只建立缺失的八个模拟账户，
不会重置余额、持仓、订单或历史绩效。

## Linux Docker Compose 部署

全新 MySQL 数据卷可直接由当前模型建表：

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps
```

已有 GuPiao MySQL 数据库必须先备份并执行增量迁移。迁移期间应停止应用写入：

```bash
docker compose stop backend worker scheduler tradingagents-worker quant-strategy-worker
docker compose exec -T mysql sh -c \
  'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"' \
  > "gupiao-before-0004-$(date +%Y%m%d-%H%M%S).sql"
docker compose exec -T mysql sh -c \
  'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"' \
  < backend/migrations/0004_independent_strategy_suite.sql
docker compose up -d backend worker scheduler tradingagents-worker quant-strategy-worker frontend
docker compose ps
```

`0004` 只用于尚未应用该版本的既有数据库，不得重复执行。升级后应先确认
`LIVE_TRADING_ENABLED=false`、`BROKER_ADAPTER=simulation`，再检查策略中心中的八个账户均为
200 万元初始本金且十六条计划仍为关闭状态。

## 启用顺序

1. 等待对应策略数据状态为完整。
2. 完成至少 500 个交易日的合格回测。
3. 对当前配置执行无下单演练。
4. 管理员在策略详情中启用该策略；服务端会按安全顺序同时打开执行和信号计划。

配置或策略版本变化后必须重新演练。任何数据缺失只暂停对应策略。

信号使用收盘后固化的真实点时数据，下一交易日才执行。财报只有公告日期时，从交易所日历
确认的下一交易日起可用；风险公告源缺失或超过新鲜度阈值时，股票策略失败关闭。

日常股票日线、复权和估值使用 Tushare 按交易日批量同步；首次建库或新证券进入 800 只池时
会单独回填历史。依赖财务的策略还需要 Tushare 财务普通接口和 `*_vip` 横截面接口权限；
权限不足时对应策略会停在 `DATA_PENDING`，不会影响其余不依赖财务的策略。

## 验证

```bash
cd backend
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .

cd ../frontend
PATH=/Users/dengbin/.nvm/versions/node/v20.19.4/bin:/usr/bin:/bin:/usr/sbin:/sbin npm run build
PATH=/Users/dengbin/.nvm/versions/node/v20.19.4/bin:/usr/bin:/bin:/usr/sbin:/sbin npm test
```
