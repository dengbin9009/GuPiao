# GuPiao Deployment Manual

## Overview

GuPiao supports two practical deployment modes:

1. Local development or single-machine runtime using the backend and frontend development servers.
2. Linux Docker Compose deployment using `mysql`, `backend`, `worker`, `scheduler`, and `frontend`.

The core system can run without any real broker gateway. LIVE trading must stay disabled until the selected adapter, account, and permissions are validated.

## Directory Layout

- `backend/`: FastAPI backend and worker logic
- `frontend/`: Vue web console
- `data/market/`: minute-bar cache and quote artifacts
- `data/backtests/`: backtest equity artifacts
- `data/plugins/`: trusted local strategy plugins
- `.env`: runtime configuration
- `docker-compose.yml`: Compose deployment entrypoint

## Prerequisites

### Local development

- Python 3.12
- Node.js 20
- npm
- Optional MySQL if you do not use SQLite

### Linux Docker Compose deployment

- Docker Engine
- Docker Compose
- Open outbound access for:
  - `frontend`: TCP `8080`
  - `backend`: TCP `8000` if you expose it directly
  - `mysql`: TCP `3306` only if external access is needed

## Environment Configuration

Create `.env` from `.env.example`:

```bash
cp .env.example .env
```

Prebuilt examples are also available:

- `.env.simulation.example`
- `.env.qmt.example`
- `.env.ptrade.example`
- `.env.futu.example`

### Minimal SIMULATION-only `.env`

```dotenv
GUPIAO_ENV=development
GUPIAO_SECRET_KEY=replace-with-a-long-random-value
GUPIAO_ADMIN_USERNAME=admin
GUPIAO_ADMIN_PASSWORD=change-me
GUPIAO_ALLOWED_IPS=127.0.0.1/32,::1/128,192.168.0.0/16
DATABASE_URL=sqlite:///./gupiao.db
CORS_ORIGINS=http://127.0.0.1:5173,http://localhost:5173,http://localhost:8080
MARKET_DATA_PROVIDER=akshare
LIVE_TRADING_ENABLED=false
BROKER_ADAPTER=simulation
```

### MySQL deployment `.env`

```dotenv
DATABASE_URL=mysql+pymysql://gupiao:change-me@mysql:3306/gupiao
```

### Optional market-data configuration

- `TUSHARE_TOKEN`
- `REALTIME_POLL_INTERVAL_SECONDS`
- `MARKET_DATA_STALE_AFTER_SECONDS`
- `CORPORATE_EVENT_SYNC_INTERVAL_SECONDS`
- `CORPORATE_EVENT_STALE_AFTER_SECONDS`

### SIMULATION risk and cost configuration

- `SIMULATION_INITIAL_CASH`
- `SIMULATION_COMMISSION_RATE`
- `SIMULATION_MIN_COMMISSION`
- `SIMULATION_STAMP_TAX_RATE`
- `SIMULATION_TRANSFER_FEE_RATE`
- `SIMULATION_SLIPPAGE_BPS`
- `SIMULATION_MAX_ORDER_NOTIONAL_ABS`
- `SIMULATION_MAX_ORDER_NOTIONAL_PCT`
- `SIMULATION_MAX_POSITION_PCT`
- `SIMULATION_MAX_TOTAL_EXPOSURE_PCT`
- `SIMULATION_DAILY_LOSS_LIMIT_PCT`
- `SIMULATION_MAX_CONSECUTIVE_ERRORS`

### LIVE configuration

Keep this disabled until verified:

```dotenv
LIVE_TRADING_ENABLED=false
```

Broker configuration options:

- QMT:
  - `QMT_GATEWAY_URL`
  - `QMT_GATEWAY_TOKEN`
- PTrade:
  - `PTRADE_GATEWAY_URL`
  - `PTRADE_GATEWAY_TOKEN`
- Futu OpenD:
  - `FUTU_OPEND_HOST`
  - `FUTU_OPEND_PORT`
  - `FUTU_TRD_MARKET`
  - `FUTU_SECURITY_FIRM`
  - `FUTU_TRD_ENV`
  - `FUTU_UNLOCK_PASSWORD`

## Local Development Startup

### Backend

```bash
cd backend
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### Frontend

```bash
cd frontend
npm run dev -- --host 127.0.0.1 --port 5173
```

Open:

- Frontend: `http://127.0.0.1:5173`
- Backend health: `http://127.0.0.1:8000/api/health`

## Docker Compose Startup

Use a deployment-oriented `.env`:

```bash
cp .env.example .env
```

Then start:

```bash
docker compose up --build -d
```

Main endpoints:

- Frontend: `http://<host>:8080`
- Backend health: `http://<host>:8000/api/health` if exposed

### Compose services

- `mysql`: persistent database
- `backend`: FastAPI API
- `worker`: notification and quote polling worker
- `scheduler`: trading-day schedule runner
- `frontend`: Nginx serving the Vue app and proxying `/api/`

## First-Time SIMULATION Setup

1. Log in with the configured administrator account.
2. Go to `特别关注`, add one or more A-share stocks.
3. Click `刷新行情`.
4. Go to `策略中心`.
5. Optionally click:
   - `同步股票主数据`
   - `同步公司事件`
   - `轮询实时报价`
6. Create a `SIMULATION` strategy configuration.
7. Run the strategy.
8. Check:
   - `账户与交易`
   - `历史回测`
   - `风控与网关`

## Enabling LIVE Trading

### Preconditions

Do not enable LIVE until all of the following are true:

- A broker adapter is configured.
- The selected gateway is healthy.
- A LIVE account is synchronized.
- The account is enabled.
- The account is not read-only.
- The account market permissions match the target securities.
- Quotes are fresh.
- LIVE risk settings are reviewed.

### QMT / PTrade / Futu workflow

1. Fill the adapter-specific `.env` values.
2. Restart the backend.
3. Go to `风控与网关`.
4. Sync LIVE accounts.
5. Verify account fields:
   - market permissions
   - capabilities
   - read-only status
6. Enable the desired account.
7. Enable LIVE mode.
8. Only then run a `LIVE` strategy configuration.

### Important fail-closed behaviors

LIVE orders are blocked when:

- no enabled account exists
- account is read-only
- account lacks `A_SHARE` market permission
- gateway is unhealthy
- quote data is stale
- event data is stale
- risk limits are exceeded

## Health Checks

### Backend

```bash
curl http://127.0.0.1:8000/api/health
```

### Provider and realtime status

After login:

- `GET /api/market-data/sources`
- `GET /api/market-data/realtime-status`
- `POST /api/market-data/realtime-poll`

### Gateway status

After login:

- `GET /api/gateways`
- `POST /api/live/accounts/sync`

## Backup

### SQLite

Back up:

- `backend/gupiao.db`
- `data/market/`
- `data/backtests/`
- `data/plugins/`

### MySQL

```bash
docker compose exec mysql mysqldump -ugupiao -p gupiao > gupiao.sql
```

Also back up:

- `data/market/`
- `data/backtests/`
- `data/plugins/`
- `.env`

## Upgrade

1. Stop traffic or announce maintenance.
2. Back up database and `data/`.
3. Pull or copy the new code.
4. Rebuild services:

```bash
docker compose up --build -d
```

5. Verify:

```bash
curl http://127.0.0.1:8000/api/health
```

6. Log in and check:
   - strategy page loads
   - realtime status page loads
   - backtests page loads
   - gateway page loads

## Rollback

1. Stop services.
2. Restore the last working code version.
3. Restore database backup.
4. Restore `data/market` and `data/backtests`.
5. Restart services.

## Troubleshooting

### Frontend opens, but data requests fail

Check:

- `backend` is running
- browser can reach `/api/`
- login cookie is valid

### Strategy says quote is stale

Check:

- `特别关注` has at least one stock
- `轮询实时报价` runs without hard errors
- `MARKET_DATA_STALE_AFTER_SECONDS` is not too low

### LIVE account sync fails

Check:

- the correct gateway is enabled
- gateway URL and token are correct
- adapter endpoint is reachable
- Futu/OpenD is running if you use Futu

### Futu adapter does not place orders

Current behavior:

- If the `futu` SDK is unavailable, the adapter fails closed.
- If the SDK is available, account query and order placement use the SDK path.

## Validation Scripts

The repository includes useful smoke and acceptance scripts:

- `backend/scripts/verify_provider_fallbacks.py`
- `backend/scripts/verify_realtime_chain.py`
- `backend/scripts/verify_schedule_chain.py`
- `backend/scripts/verify_backtest_acceptance.py`
- `backend/scripts/verify_live_guardrails.py`
- `backend/scripts/verify_live_symbol_permissions.py`
- `backend/scripts/verify_futu_adapter.py`
- `backend/scripts/verify_adapter_contracts.py`
- `backend/scripts/verify_cross_platform_smoke.py`
- `backend/scripts/verify_e2e_smoke.py`
