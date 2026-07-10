# GuPiao Go-Live Checklist

## Purpose

Use this checklist before promoting GuPiao from development or staging into a real deployment window.

This checklist assumes:

- `DEPLOYMENT.md` is the deployment reference.
- `LIVE` remains disabled by default.
- a real broker gateway is optional and must be validated separately.

## 1. Pre-Deployment Basics

- [ ] Confirm the target environment: local, test host, or Linux Docker Compose.
- [ ] Confirm the deployment date and rollback window.
- [ ] Confirm the current repository state is the intended release state.
- [ ] Confirm `.env` exists on the target host.
- [ ] Confirm `GUPIAO_SECRET_KEY` is not the development placeholder.
- [ ] Confirm `GUPIAO_ADMIN_PASSWORD` is not the development default.
- [ ] Confirm `GUPIAO_ALLOWED_IPS` is restricted to intended networks.
- [ ] Confirm `DATABASE_URL` points to the intended runtime database.
- [ ] Confirm `LIVE_TRADING_ENABLED=false` unless you explicitly plan a real broker cutover.

## 2. Data and Backup

- [ ] Back up the database before deployment.
- [ ] Back up `data/market/`.
- [ ] Back up `data/backtests/`.
- [ ] Back up `data/plugins/` if plugins are in use.
- [ ] Confirm backup files are readable and not zero bytes.
- [ ] Record backup location and timestamp.

## 3. Service Build and Startup

- [ ] Build frontend successfully:
  - `cd frontend && npm run build`
- [ ] Start backend successfully:
  - `cd backend && .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
  - or `docker compose up --build -d`
- [ ] Confirm backend health:
  - `curl http://127.0.0.1:8000/api/health`
- [ ] Confirm frontend is reachable:
  - `http://127.0.0.1:5173` for dev
  - `http://<host>:8080` for Docker Compose
- [ ] Confirm worker starts without import/runtime errors.
- [ ] Confirm scheduler starts without import/runtime errors.

## 4. SIMULATION Pre-Go-Live Verification

- [ ] Log in with the intended administrator account.
- [ ] Add or verify at least one stock in `特别关注`.
- [ ] Run `同步股票主数据`.
- [ ] Run `同步公司事件`.
- [ ] Run `轮询实时报价`.
- [ ] Confirm `关注股实时状态` shows expected quote time and stale status.
- [ ] Create a `SIMULATION` strategy configuration.
- [ ] Run a simulation strategy once.
- [ ] Confirm strategy result is visible in `策略中心`.
- [ ] Confirm simulation account, orders, positions, and risk events load correctly.
- [ ] Run one historical backtest.
- [ ] Open a backtest detail and confirm metrics, trade list, and equity curve are visible.

## 5. Market Data Checks

- [ ] Confirm `AKShare` provider status is visible.
- [ ] Confirm `Tushare` status matches whether a token is configured.
- [ ] Confirm realtime quote stale threshold is visible and matches configuration.
- [ ] Confirm `data/market/*.parquet` files are created after requesting minute bars.
- [ ] Confirm `data/backtests/*.parquet` files are created after running backtests.
- [ ] Confirm stale quote behavior blocks strategy execution when quote timestamps are missing or too old.

## 6. Notification Checks

- [ ] Confirm at least one notification channel is configured if alerts are required.
- [ ] Run a notification test send.
- [ ] Confirm the delivery record is created.
- [ ] Confirm notification failure does not block the application.

## 7. LIVE Readiness Checks

Skip this section if the release is simulation-only.

- [ ] Confirm the intended adapter type is configured:
  - `QMT`
  - `PTrade`
  - `Futu OpenD`
- [ ] Confirm gateway URL/port/token fields are present in `.env`.
- [ ] Confirm `风控与网关` page loads successfully.
- [ ] Sync LIVE accounts.
- [ ] Confirm at least one account appears.
- [ ] Confirm the account is not read-only.
- [ ] Confirm the account market permissions include the intended market.
- [ ] Confirm gateway health is visible and healthy.
- [ ] Confirm LIVE risk settings are reviewed.
- [ ] Confirm enabling LIVE requires explicit action.
- [ ] Confirm LIVE orders still fail closed when:
  - account is missing
  - account is read-only
  - account lacks A-share permission
  - gateway is unhealthy
  - quotes are stale

## 8. Release Window Execution

- [ ] Stop any previous deployment cleanly.
- [ ] Apply the intended code version.
- [ ] Apply the intended `.env`.
- [ ] Start services.
- [ ] Re-run backend health check.
- [ ] Re-run frontend reachability check.
- [ ] Log in and load:
  - dashboard
  - strategy center
  - backtests
  - risk/gateway page
- [ ] Confirm no blocking runtime error appears in logs.

## 9. Post-Deployment Verification

- [ ] Confirm login works.
- [ ] Confirm `特别关注` loads.
- [ ] Confirm market data status loads.
- [ ] Confirm strategy config list loads.
- [ ] Confirm historical backtest list loads.
- [ ] Confirm account and order pages load.
- [ ] Confirm risk events page loads.
- [ ] Confirm notifications page loads.
- [ ] Confirm worker-driven realtime polling is still functioning.

## 10. Rollback Triggers

Rollback if any of the following occur:

- [ ] Backend health check fails repeatedly.
- [ ] Frontend cannot load or repeatedly errors on startup.
- [ ] Login is broken.
- [ ] Strategy execution crashes.
- [ ] Realtime data state is permanently unavailable.
- [ ] Account or gateway pages error out.
- [ ] Unexpected LIVE eligibility or permission behavior appears.

## 11. Rollback Steps

- [ ] Stop current services.
- [ ] Restore the last known-good code version.
- [ ] Restore the database backup.
- [ ] Restore `data/market/`, `data/backtests/`, and `data/plugins/`.
- [ ] Restart services.
- [ ] Re-run:
  - backend health check
  - frontend reachability check
  - login smoke check

## 12. Evidence to Keep

- [ ] Save build logs.
- [ ] Save deployment timestamp.
- [ ] Save health check outputs.
- [ ] Save backup locations.
- [ ] Save any LIVE adapter validation notes.

