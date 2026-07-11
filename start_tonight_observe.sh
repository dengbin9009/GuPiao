#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
ENV_FILE="$ROOT/.env"
RUN_DIR="$ROOT/.run"

BACKEND_LOG="$BACKEND_DIR/.uvicorn.log"
WORKER_LOG="$BACKEND_DIR/.worker.log"
SCHEDULER_LOG="$BACKEND_DIR/.scheduler.log"
FRONTEND_LOG="$FRONTEND_DIR/.vite.log"
BACKEND_PID_FILE="$RUN_DIR/backend.pid"
WORKER_PID_FILE="$RUN_DIR/worker.pid"
SCHEDULER_PID_FILE="$RUN_DIR/scheduler.pid"
FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"

PYTHON_BIN="${GUPIAO_PYTHON_BIN:-$BACKEND_DIR/.venv/bin/python}"

echo "== GuPiao tonight observe mode =="

mkdir -p "$ROOT/data/market" "$ROOT/data/backtests" "$ROOT/data/plugins" "$RUN_DIR"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Missing backend venv python: $PYTHON_BIN"
  exit 1
fi

NODE_BIN="${GUPIAO_NODE_BIN:-}"
if [ -z "$NODE_BIN" ]; then
  for candidate in "$HOME"/.nvm/versions/node/v20.*/bin; do
    if [ -x "$candidate/node" ]; then
      NODE_BIN="$candidate"
      break
    fi
  done
fi
if [ -z "$NODE_BIN" ] && command -v node >/dev/null 2>&1; then
  if [ "$(node -p 'process.versions.node.split(`.`)[0]' 2>/dev/null || true)" = "20" ]; then
    NODE_BIN="$(dirname "$(command -v node)")"
  fi
fi
if [ -z "$NODE_BIN" ] || [ ! -x "$NODE_BIN/node" ]; then
  echo "Node.js 20 is required"
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
MARKET_DATA_STALE_AFTER_SECONDS=60
CORPORATE_EVENT_PROVIDERS=cninfo,tushare,akshare
CORPORATE_EVENT_SYNC_INTERVAL_SECONDS=300
CORPORATE_EVENT_STALE_AFTER_SECONDS=1800

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
  echo "Administrator password was written to the local .env file"
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [ "${LIVE_TRADING_ENABLED:-false}" != "false" ] || [ "${BROKER_ADAPTER:-simulation}" != "simulation" ]; then
  echo "Observe mode requires LIVE_TRADING_ENABLED=false and BROKER_ADAPTER=simulation"
  exit 1
fi

OBSERVE_INITIAL_CASH="${GUPIAO_OBSERVE_INITIAL_CASH:-100000}"
if ! "$PYTHON_BIN" -c 'import sys; value = float(sys.argv[1]); raise SystemExit(0 if value > 0 else 1)' "$OBSERVE_INITIAL_CASH"; then
  echo "GUPIAO_OBSERVE_INITIAL_CASH must be a positive number"
  exit 1
fi
export SIMULATION_INITIAL_CASH="$OBSERVE_INITIAL_CASH"
export MARKET_DATA_STALE_AFTER_SECONDS="${GUPIAO_OBSERVE_MARKET_STALE_SECONDS:-60}"
export CORPORATE_EVENT_STALE_AFTER_SECONDS="${GUPIAO_OBSERVE_EVENT_STALE_SECONDS:-1800}"

process_cwd() {
  local pid="$1"
  if [ -e "/proc/$pid/cwd" ]; then
    readlink "/proc/$pid/cwd" 2>/dev/null || true
  elif command -v lsof >/dev/null 2>&1; then
    lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1
  fi
}

stop_managed_pid() {
  local pid_file="$1"
  local expected_dir="$2"
  local pid=""
  local cwd=""
  if [ -f "$pid_file" ]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      cwd="$(process_cwd "$pid")"
      case "$cwd/" in
        "$expected_dir/"*) kill "$pid" 2>/dev/null || true ;;
        *) echo "Refusing to stop PID $pid: process is not owned by $expected_dir" ;;
      esac
    fi
    rm -f "$pid_file"
  fi
}

stop_managed_pid "$BACKEND_PID_FILE" "$BACKEND_DIR"
stop_managed_pid "$WORKER_PID_FILE" "$BACKEND_DIR"
stop_managed_pid "$SCHEDULER_PID_FILE" "$BACKEND_DIR"
stop_managed_pid "$FRONTEND_PID_FILE" "$FRONTEND_DIR"

cd "$BACKEND_DIR"
"$PYTHON_BIN" scripts/prepare_simulation_runtime.py

start_bg() {
  local pid_file="$1"
  local log_file="$2"
  shift 2
  nohup "$@" > "$log_file" 2>&1 &
  LAST_PID=$!
  STARTED_PIDS+=("$LAST_PID")
  echo "$LAST_PID" > "$pid_file"
}

STARTED_PIDS=()
cleanup_started() {
  local pid
  for pid in "${STARTED_PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup_started EXIT INT TERM

cd "$BACKEND_DIR"
start_bg "$BACKEND_PID_FILE" "$BACKEND_LOG" "$PYTHON_BIN" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
BACKEND_PID=$LAST_PID
start_bg "$WORKER_PID_FILE" "$WORKER_LOG" "$PYTHON_BIN" -m app.worker
WORKER_PID=$LAST_PID
start_bg "$SCHEDULER_PID_FILE" "$SCHEDULER_LOG" "$PYTHON_BIN" -m app.scheduler_runner
SCHEDULER_PID=$LAST_PID

cd "$FRONTEND_DIR"
PATH="$NODE_BIN:$PATH"
start_bg "$FRONTEND_PID_FILE" "$FRONTEND_LOG" npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
FRONTEND_PID=$LAST_PID

sleep 4

for process in "backend:$BACKEND_PID:$BACKEND_LOG" "worker:$WORKER_PID:$WORKER_LOG" "scheduler:$SCHEDULER_PID:$SCHEDULER_LOG" "frontend:$FRONTEND_PID:$FRONTEND_LOG"; do
  IFS=: read -r name pid log_file <<< "$process"
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "$name failed to start; inspect $log_file"
    exit 1
  fi
done

/usr/bin/curl -fsS http://127.0.0.1:8000/api/health >/dev/null
/usr/bin/curl -fsSI http://127.0.0.1:5173 >/dev/null

if [ "${GUPIAO_ATTACHED:-false}" = "true" ]; then
  VERIFY_SECONDS="${GUPIAO_VERIFY_SECONDS:-60}"
  VERIFIED=false
  ATTACHED_STARTED_AT=$SECONDS
  while true; do
    for process in "backend:$BACKEND_PID:$BACKEND_LOG" "worker:$WORKER_PID:$WORKER_LOG" "scheduler:$SCHEDULER_PID:$SCHEDULER_LOG" "frontend:$FRONTEND_PID:$FRONTEND_LOG"; do
      IFS=: read -r name pid log_file <<< "$process"
      if ! kill -0 "$pid" 2>/dev/null; then
        echo "$name stopped; inspect $log_file"
        exit 1
      fi
    done
    if [ "$VERIFIED" = "false" ] && [ $((SECONDS - ATTACHED_STARTED_AT)) -ge "$VERIFY_SECONDS" ]; then
      /usr/bin/curl -fsS http://127.0.0.1:8000/api/health >/dev/null
      /usr/bin/curl -fsSI http://127.0.0.1:5173 >/dev/null
      echo "Runtime verification passed after ${VERIFY_SECONDS}s"
      VERIFIED=true
    fi
    sleep 5
  done
fi

trap - EXIT INT TERM

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
