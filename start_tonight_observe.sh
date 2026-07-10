#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/dengbin/Code/github/GuPiao"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
ENV_FILE="$ROOT/.env"
BACKEND_DB="$BACKEND_DIR/gupiao.db"
RUN_DIR="$ROOT/.run"

BACKEND_LOG="$BACKEND_DIR/.uvicorn.log"
WORKER_LOG="$BACKEND_DIR/.worker.log"
SCHEDULER_LOG="$BACKEND_DIR/.scheduler.log"
FRONTEND_LOG="$FRONTEND_DIR/.vite.log"
BACKEND_PID_FILE="$RUN_DIR/backend.pid"
WORKER_PID_FILE="$RUN_DIR/worker.pid"
SCHEDULER_PID_FILE="$RUN_DIR/scheduler.pid"
FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"

PYTHON_BIN="$BACKEND_DIR/.venv/bin/python"
NODE_BIN="/Users/dengbin/.nvm/versions/node/v20.19.4/bin"

echo "== GuPiao tonight observe mode =="

mkdir -p "$ROOT/data/market" "$ROOT/data/backtests" "$ROOT/data/plugins" "$RUN_DIR"

if [ ! -f "$PYTHON_BIN" ]; then
  echo "Missing backend venv python: $PYTHON_BIN"
  exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
  umask 077
  GENERATED_SECRET="$(/usr/bin/openssl rand -hex 32)"
  GENERATED_PASSWORD="$(/usr/bin/openssl rand -hex 12)"
  cat > "$ENV_FILE" <<EOF
GUPIAO_ENV=development
GUPIAO_SECRET_KEY=$GENERATED_SECRET
GUPIAO_ADMIN_USERNAME=admin
GUPIAO_ADMIN_PASSWORD=$GENERATED_PASSWORD
GUPIAO_ALLOWED_IPS=127.0.0.1/32,::1/128,192.168.0.0/16
DATABASE_URL=sqlite:///./gupiao.db
CORS_ORIGINS=http://127.0.0.1:5173,http://localhost:5173,http://localhost:8080

MARKET_DATA_PROVIDER=akshare
TUSHARE_TOKEN=
REALTIME_POLL_INTERVAL_SECONDS=5
MARKET_DATA_STALE_AFTER_SECONDS=86400
CORPORATE_EVENT_PROVIDERS=cninfo,tushare,akshare
CORPORATE_EVENT_SYNC_INTERVAL_SECONDS=300
CORPORATE_EVENT_STALE_AFTER_SECONDS=172800

SIMULATION_INITIAL_CASH=100000
SIMULATION_COMMISSION_RATE=0.0003
SIMULATION_MIN_COMMISSION=5
SIMULATION_STAMP_TAX_RATE=0.0005
SIMULATION_TRANSFER_FEE_RATE=0
SIMULATION_SLIPPAGE_BPS=5
SIMULATION_MAX_ORDER_NOTIONAL_ABS=20000
SIMULATION_MAX_ORDER_NOTIONAL_PCT=0.20
SIMULATION_MAX_POSITION_PCT=0.20
SIMULATION_MAX_TOTAL_EXPOSURE_PCT=0.60
SIMULATION_DAILY_LOSS_LIMIT_PCT=0.03
SIMULATION_MAX_CONSECUTIVE_ERRORS=3

LIVE_TRADING_ENABLED=false
LIVE_MAX_ORDER_NOTIONAL_ABS=5000
LIVE_MAX_ORDER_NOTIONAL_PCT=0.05
LIVE_MAX_POSITION_PCT=0.10
LIVE_MAX_TOTAL_EXPOSURE_PCT=0.30
LIVE_DAILY_LOSS_LIMIT_PCT=0.01
LIVE_MAX_CONSECUTIVE_ERRORS=3
LIVE_MAX_DAILY_ORDERS=5

BROKER_ADAPTER=simulation
QMT_GATEWAY_URL=
QMT_GATEWAY_TOKEN=
PTRADE_GATEWAY_URL=
PTRADE_GATEWAY_TOKEN=
FUTU_OPEND_HOST=127.0.0.1
FUTU_OPEND_PORT=11111
FUTU_TRD_MARKET=HK
FUTU_SECURITY_FIRM=FUTUSECURITIES
FUTU_TRD_ENV=SIMULATE
FUTU_UNLOCK_PASSWORD=

SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM=
NOTIFICATION_EMAIL_TO=
WECOM_WEBHOOK_URL=
EOF
  echo "Created $ENV_FILE with random local credentials"
  echo "Administrator username: admin"
  echo "Administrator password: $GENERATED_PASSWORD"
fi

for pid_file in "$BACKEND_PID_FILE" "$WORKER_PID_FILE" "$SCHEDULER_PID_FILE" "$FRONTEND_PID_FILE"; do
  if [ -f "$pid_file" ]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [ -n "${pid:-}" ]; then
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$pid_file"
  fi
done

pkill -f "uvicorn app.main:app" 2>/dev/null || true
pkill -f "python -m app.worker" 2>/dev/null || true
pkill -f "python -m app.scheduler_runner" 2>/dev/null || true
pkill -f "vite --host 127.0.0.1 --port 5173" 2>/dev/null || true

sleep 1

/usr/bin/sqlite3 "$BACKEND_DB" <<'SQL'
update simulation_accounts
set initial_cash=100000,
    cash_balance=100000,
    available_cash=100000,
    frozen_cash=0,
    total_asset=100000,
    realized_pnl=0,
    unrealized_pnl=0
where id=1;

insert into simulation_account_ledgers (simulation_account_id,event_type,amount,balance_after,message,created_at)
values (1,'adjustment',100000,100000,'tonight observe mode set to 10w',datetime('now'));

insert or ignore into watchlist_items (stock_id,note,created_at)
select id,'tonight observe default',datetime('now') from stocks where symbol='000001.SZ';

update stocks
set quote_updated_at=datetime('now'),
    updated_at=datetime('now')
where symbol='000001.SZ';

update data_source_states
set healthy=1,
    last_checked_at=datetime('now'),
    last_quote_at=datetime('now'),
    last_error=NULL
where provider in ('akshare','cninfo');

update strategy_schedules set enabled=1 where strategy_config_id=2;
SQL

echo "Simulation account reset to 100000, watchlist prepared, quote timestamps refreshed, and strategy_config_id=2 schedules enabled"

export $(grep -v '^#' "$ENV_FILE" | xargs)

start_bg() {
  local pid_file="$1"
  local log_file="$2"
  shift 2
  nohup "$@" > "$log_file" 2>&1 &
  local pid=$!
  echo "$pid" > "$pid_file"
  echo "$pid"
}

cd "$BACKEND_DIR"
BACKEND_PID=$(start_bg "$BACKEND_PID_FILE" "$BACKEND_LOG" "$PYTHON_BIN" -m uvicorn app.main:app --host 127.0.0.1 --port 8000)
WORKER_PID=$(start_bg "$WORKER_PID_FILE" "$WORKER_LOG" "$PYTHON_BIN" -m app.worker || true)
SCHEDULER_PID=$(start_bg "$SCHEDULER_PID_FILE" "$SCHEDULER_LOG" "$PYTHON_BIN" -m app.scheduler_runner || true)

cd "$FRONTEND_DIR"
PATH="$NODE_BIN:$PATH"
FRONTEND_PID=$(start_bg "$FRONTEND_PID_FILE" "$FRONTEND_LOG" npm run dev -- --host 127.0.0.1 --port 5173)

sleep 4

if ! /usr/bin/curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
  echo "Backend health check failed, inspect $BACKEND_LOG"
fi

if ! /usr/bin/curl -fsSI http://127.0.0.1:5173 >/dev/null 2>&1; then
  echo "Frontend health check failed, inspect $FRONTEND_LOG"
fi

echo "Backend PID:   $BACKEND_PID"
echo "Worker PID:    $WORKER_PID"
echo "Scheduler PID: $SCHEDULER_PID"
echo "Frontend PID:  $FRONTEND_PID"

echo
echo "Logs:"
echo "  $BACKEND_LOG"
echo "  $WORKER_LOG"
echo "  $SCHEDULER_LOG"
echo "  $FRONTEND_LOG"

echo
echo "Check:"
echo "  curl http://127.0.0.1:8000/api/health"
echo "  open http://127.0.0.1:5173"
echo
echo "Tomorrow morning, check:"
echo "  策略中心 -> 最近运行"
echo "  账户与交易"
echo "  历史回测"
