# GuPiao

GuPiao is a Chinese A-share quantitative trading console built from the SpecKit documents in `specs/001-gupiao`.

## Implementation status

The current implementation has completed 99 of 99 tasks from `specs/001-gupiao/tasks.md`. Implemented areas include authentication, 26-table persistence, AKShare/Tushare market-data providers with fallback routing, stock/quote/event synchronization, minute-bar Parquet caching, watchlist/search, market-data freshness gates, built-in and isolated plugin strategy registration, the overnight strategy entry/exit path and filters, simulation accounting with append-only audit records, risk gates, fail-closed LIVE broker handoff and masked account sync, schedules, routed notifications, historical backtesting, gateway capability views, and the Chinese Vue console.

LIVE trading remains disabled by default. Before using a real broker account, configure and validate the chosen adapter, credentials, and market permissions in your own environment.

## Local development

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

Open `http://localhost:5173`. The development default administrator is configured through `GUPIAO_ADMIN_USERNAME` and `GUPIAO_ADMIN_PASSWORD`.

When frontend dependencies are unavailable, `frontend/preview.html` provides a static visual preview only; it is not the running application.

## Docker Compose

```bash
cp .env.example .env
docker compose up --build
```

Open `http://localhost:8080`. LIVE trading is disabled by default and remains unavailable until a supported broker adapter is explicitly configured and healthy.

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

- 默认优先读取 `data/market/<symbol>-1m.parquet` 本地缓存
- 如果缓存覆盖不足，脚本会尝试通过已配置分钟线数据源补拉
- 如果补拉后仍然覆盖不足，脚本会直接失败，不会回退到演示生成 K 线
- 输出同时包含 JSON 结果和一行便于人工查看的收益摘要
