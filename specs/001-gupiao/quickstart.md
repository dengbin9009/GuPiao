# Quickstart: GuPiao

## Prerequisites

- macOS or Windows for local development/runtime, or Linux for Docker Compose deployment.
- Python 3.11+.
- Node.js 20+ for Vue development.
- Docker and Docker Compose.
- MySQL, preferably through Docker Compose.
- Optional Tushare token.
- Optional remote QMT, broker-hosted PTrade, or Futu OpenD adapter for LIVE trading.

## Environment Variables

```bash
GUPIAO_ENV=development
GUPIAO_SECRET_KEY=change-me
GUPIAO_ADMIN_USERNAME=admin
GUPIAO_ADMIN_PASSWORD=replace-with-a-strong-password
GUPIAO_ALLOWED_IPS=127.0.0.1,192.168.0.0/16

DATABASE_URL=mysql+pymysql://gupiao:change-me@mysql:3306/gupiao

MARKET_DATA_PROVIDER=akshare
TUSHARE_TOKEN=
CORPORATE_EVENT_PROVIDERS=cninfo,tushare,akshare
CORPORATE_EVENT_SYNC_INTERVAL_SECONDS=300
CORPORATE_EVENT_STALE_AFTER_SECONDS=1800
REALTIME_POLL_INTERVAL_SECONDS=5
MARKET_DATA_STALE_AFTER_SECONDS=15
MARKET_DATA_ARTIFACT_DIR=/data/market
BACKTEST_ARTIFACT_DIR=/data/backtests
TRADING_TIMEZONE=Asia/Shanghai
TRUSTED_PLUGIN_DIR=/data/plugins

SIMULATION_INITIAL_CASH=10000
SIMULATION_COMMISSION_RATE=0.0003
SIMULATION_MIN_COMMISSION=5
SIMULATION_STAMP_TAX_RATE=0.0005
SIMULATION_TRANSFER_FEE_RATE=0
SIMULATION_SLIPPAGE_BPS=5

SIMULATION_MAX_ORDER_NOTIONAL_ABS=2000
SIMULATION_MAX_ORDER_NOTIONAL_PCT=0.20
SIMULATION_MAX_POSITION_PCT=0.20
SIMULATION_MAX_TOTAL_EXPOSURE_PCT=0.60
SIMULATION_DAILY_LOSS_LIMIT_PCT=0.03
SIMULATION_MAX_CONSECUTIVE_ERRORS=3

LIVE_MAX_ORDER_NOTIONAL_ABS=5000
LIVE_MAX_ORDER_NOTIONAL_PCT=0.05
LIVE_MAX_POSITION_PCT=0.10
LIVE_MAX_TOTAL_EXPOSURE_PCT=0.30
LIVE_DAILY_LOSS_LIMIT_PCT=0.01
LIVE_MAX_CONSECUTIVE_ERRORS=3
LIVE_MAX_DAILY_ORDERS=5

LIVE_TRADING_ENABLED=false
BROKER_ADAPTER=simulation
QMT_GATEWAY_URL=
QMT_GATEWAY_TOKEN=
PTRADE_GATEWAY_URL=
PTRADE_GATEWAY_TOKEN=
FUTU_OPEND_HOST=127.0.0.1
FUTU_OPEND_PORT=11111

SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM=
NOTIFICATION_EMAIL_TO=
WECOM_WEBHOOK_URL=
```

## macOS and Windows Development

1. Copy `.env.example` to `.env`; for a no-Docker local database, set `DATABASE_URL=sqlite:///./gupiao.db`.
2. In `backend`, run `.venv/bin/python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`.
3. In `frontend`, run `npm install` and `npm run dev -- --host 127.0.0.1 --port 5173`.
4. Open the LAN/local web console.
5. Log in as the administrator.
6. Use SIMULATION mode until strategies and risk settings are validated.
7. Confirm the default CNY 10,000 simulation account is created automatically on first SIMULATION use.
8. Run a historical backtest before enabling any strategy schedule.

LIVE trading is not available unless the selected BrokerAdapter is configured, healthy, and entitled for the target securities.

## Linux Docker Compose Deployment

1. Copy `.env.example` to `.env` on the Linux host.
2. Set strong administrator and database secrets.
3. Configure `TUSHARE_TOKEN` if Tushare is enabled.
4. Leave `LIVE_TRADING_ENABLED=false` until the selected broker adapter and risk settings are verified.
5. Run `docker compose up --build -d`.
6. Verify backend, frontend, MySQL, worker, data source, and gateway health.

Back up both stores: use `docker compose exec mysql mysqldump -ugupiao -p gupiao` for MySQL and archive the host `data/market` plus `data/backtests` directories for Parquet artifacts. Restore and test backups before enabling LIVE mode.

## Broker Adapter Behavior

- GuPiao core services run independently on macOS, Windows, and Linux.
- QMT/miniQMT remains available through a remote Windows gateway for mainland broker access.
- PTrade is available through a broker-hosted integration when the broker grants API access.
- Futu OpenD can run on macOS, Windows, or Linux; A-share eligibility depends on the user's account region and permissions.
- LIVE accounts are real broker accounts exposed by the selected adapter and stored in GuPiao only as masked mappings.
- If the selected adapter is empty, unreachable, unhealthy, unauthorized, or not entitled for a symbol, all affected LIVE orders must be blocked.
- If a LIVE account mapping is disabled or read-only, all LIVE orders for that account must be blocked.
- Backtest and SIMULATION modes must continue working when no LIVE adapter is configured.

## Simulation Account Behavior

- v1 creates one default simulation account automatically on first SIMULATION use.
- Default initial cash is controlled by `SIMULATION_INITIAL_CASH`.
- Simulated fills apply commission, minimum commission, stamp tax, transfer fee, and slippage settings.
- Simulation reset requires administrator authorization and is blocked while strategy runs or open orders exist.

## Backtest and Schedule Behavior

- Every strategy declares supported timeframes. The built-in overnight strategy currently accepts minute (`1m`) backtests only.
- Strategy schedules are disabled by default and run only on exchange trading days in `Asia/Shanghai`.
- "一夜持股法" uses separate entry evaluation at 14:40 and next-trading-day exit evaluation at 09:35.
- Missed schedule windows are skipped and never replayed as late orders.
- Tail-session quotes poll every `REALTIME_POLL_INTERVAL_SECONDS`; automated orders are blocked when quotes exceed `MARKET_DATA_STALE_AFTER_SECONDS`.
- The overnight strategy requires `1m` data; daily-only requests are rejected.
- Corporate events sync every `CORPORATE_EVENT_SYNC_INTERVAL_SECONDS`; missing data older than `CORPORATE_EVENT_STALE_AFTER_SECONDS` rejects event-filtered candidates.

## Notification Behavior

- Email uses the configured SMTP settings.
- Enterprise WeChat uses the configured group-bot webhook.
- Notification events include order success/failure, risk block/circuit breaker, gateway offline/recovery, strategy failure, and daily summary.
- Delivery failures are retried up to three times, recorded, and never block trading.

## Safe First Run

1. Keep `LIVE_TRADING_ENABLED=false`.
2. Sync stock master data.
3. Search stocks by code, Chinese name, and pinyin initials.
4. Add several stocks to the special watchlist.
5. Review the configurable "一夜持股法" defaults from `spec.md`.
6. Run a minute-data backtest and review trades, metrics, drawdown, and equity curve.
7. Run the strategy manually in SIMULATION mode.
8. Confirm the simulation account, ledger, orders, fills, positions, snapshots, strategy logs, and risk events are recorded.
9. Configure and test email and Enterprise WeChat notifications.
10. Enable a trading-day schedule only after manual and simulated runs pass.
11. Only after backtest and simulation validation, configure broker health checks and LIVE risk limits.
