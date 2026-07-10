# GuPiao

GuPiao 是一个根据 `specs/001-gupiao` 中 SpecKit 文档开发的 A 股量化交易控制台。

## 实现状态

当前已完成 `specs/001-gupiao/tasks.md` 中的 99 项任务。实现范围包括：登录认证、26 张业务表、AKShare/Tushare/mootdx 行情路由、股票与行情同步、分钟和小时 K 线缓存、关注列表与股票搜索、数据新鲜度门禁、内置策略和隔离的 Python 插件策略、一夜持股法的进出场链路、模拟盘记账、不可删除的审计记录、强制风控、实盘失败关闭、定时执行、邮件与企业微信通知、历史回测、交易网关状态和中文 Vue 控制台。

真实盘默认并持续关闭。连接真实券商前，必须单独配置并验证适配器、凭据、行情权限和风控参数；Loop 自动流程不得启用真实盘或发送真实订单。

## 一键启动模拟盘观察版

首次运行会创建本地 `.env`，以随机密码初始化管理员，并幂等准备 10 万元模拟账户、`000001.SZ` 关注项、一夜持股法模拟配置及进出场计划。脚本会强制检查 `LIVE_TRADING_ENABLED=false` 和 `BROKER_ADAPTER=simulation`。

观察版每次启动都会把模拟初始资金调整为 10 万元；可通过 `GUPIAO_OBSERVE_INITIAL_CASH` 修改。种子股票报价不会被当作实时行情，外部行情或公司事件数据未通过实际探测时，策略会记录原因并禁止下单。

```bash
./start_tonight_observe.sh
```

启动成功后访问 `http://127.0.0.1:5173`。进程号写入 `.run/`，日志分别位于 `backend/.uvicorn.log`、`backend/.worker.log`、`backend/.scheduler.log` 和 `frontend/.vite.log`。

需要在终端前台持续观察并完成 60 秒存活验证时：

```bash
GUPIAO_ATTACHED=true ./start_tonight_observe.sh
```

## 本地开发

```bash
cp .env.example .env
cd backend
.venv/bin/python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

In another terminal:

```bash
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5173
```

打开 `http://localhost:5173`。开发环境管理员由 `GUPIAO_ADMIN_USERNAME` 和 `GUPIAO_ADMIN_PASSWORD` 配置。

前端依赖不可用时，`frontend/preview.html` 只能用于静态预览，不代表应用已经运行。

## Docker Compose 部署

```bash
cp .env.example .env
docker compose up --build
```

打开 `http://localhost:8080`。真实盘默认关闭；只有受支持的交易适配器配置完成并通过健康检查后，才能进入后续人工验收流程。

## 最近两日隔夜回测脚本

可以直接运行后端脚本，检查某只股票在指定两日之间的隔夜收益表现：

```bash
cd backend
.venv/bin/python scripts/backtest_recent_overnight.py \
  --symbol 000001.SZ \
  --entry-date 2026-06-24 \
  --exit-date 2026-06-25
```

说明：

- 默认优先读取 `data/market/<symbol>-1m.parquet` 本地缓存并通过已配置数据源补拉
- 1 分钟行情失败、权限不足或覆盖不全时，自动退化到真实的 60 分钟行情
- 也可以传入 `--preferred-timeframe 60m` 直接使用小时线
- 60 分钟行情仍不可用时脚本明确失败，不会用演示生成 K 线冒充真实回测
- 输出同时包含 JSON 结果和一行便于人工查看的收益摘要
