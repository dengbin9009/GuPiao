# Implementation Plan: GuPiao Quant Trading Console

**Branch**: `001-gupiao`  
**Spec**: `specs/001-gupiao/spec.md`  
**Created**: 2026-06-22

## Summary

Build GuPiao as a LAN-accessible A-share quantitative trading web console. The core runs on macOS and Windows for local use and Linux through Docker Compose. FastAPI and Vue provide the application, MySQL stores business records, Parquet stores versioned minute-data/equity artifacts, AKShare and Tushare provide market data, Backtrader powers event-driven backtests, and pluggable BrokerAdapters route simulation and LIVE trading.

## Technical Context

- **Backend**: Python, FastAPI, Pydantic, SQLAlchemy/Alembic.
- **Frontend**: Vue with a Chinese admin console.
- **Database**: MySQL.
- **Artifact Storage**: Parquet files for minute-bar caches and backtest equity curves.
- **Market Data**: AKShare and Tushare behind MarketDataProvider plus CNINFO/Tushare/AKShare behind CorporateEventProvider, with minute, real-time, and event freshness metadata.
- **Backtesting**: Backtrader with GuPiao A-share commission, T+1, lot-size, suspension, and price-limit extensions.
- **Scheduling**: APScheduler-compatible persistent jobs in `Asia/Shanghai`, guarded by exchange trading calendars and idempotent schedule-window claims.
- **Trading**: BrokerAdapter interface with simulation, remote QMT, PTrade, and Futu OpenD adapter paths.
- **Notifications**: SMTP email and Enterprise WeChat webhook workers with append-only delivery logs.
- **Deployment**: Docker Compose for Linux production and local services.
- **Security**: Single administrator login, session timeout, IP allowlist, environment-based secrets.
- **Strategy Types**: Built-in parameterized strategies and custom Python plugin strategies.

## Constitution Check

- LIVE trading is disabled by default and fails closed on uncertainty.
- SIMULATION and LIVE share the same signal, risk, order, and logging path.
- Every order passes mandatory risk controls before broker submission.
- Trading records are append-only from the application perspective.
- macOS/Windows/Linux core runtime does not depend on a single broker runtime.
- Historical backtests apply the same cost, slippage, and A-share execution assumptions as simulation.

## Architecture

### Backend Modules

- **Auth**: Administrator login, session management, IP allowlist checks.
- **Stocks**: Stock master synchronization, quote lookup, code/name/pinyin search.
- **Watchlist**: Special watchlist CRUD and quote refresh.
- **Market Data**: AKShare/Tushare market adapters, normalized corporate-event providers, source health, and freshness state.
- **Strategies**: Strategy registry, parameter validation, built-in strategies, plugin loader.
- **Backtesting**: Backtrader integration, reproducibility metadata, trade/metric persistence, Parquet artifacts.
- **Execution**: Manual runner, entry/exit/custom trading-day triggers, signal normalization, mode selection.
- **Risk**: Per-order limits, exposure limits, daily loss circuit breaker, consecutive error pause, emergency stop.
- **Accounts**: Default simulation account, LIVE account masked mapping, account snapshots, simulation ledger.
- **Broker**: Simulation, QMT gateway, PTrade, and Futu OpenD adapters with normalized order/fill/position/account types.
- **Notifications**: Email/Enterprise WeChat channel configuration, rendering, retry, and delivery logs.
- **Audit**: Append-only logs for strategy runs, orders, fills, simulation ledger entries, risk events, and gateway events.

### Frontend Views

- Dashboard: mode, gateway, risk, strategy, and recent event summary.
- Login: administrator access.
- Special Watchlist: search by code/name/pinyin, add/remove followed stocks, quote refresh.
- Strategy Center: built-in strategy selection, parameter editing, plugin strategy status.
- Backtests: configuration, run status, metrics, equity curve, and trades.
- Schedules: manual run action and trading-day schedule management.
- Strategy Runs: run history, logs, generated signals, mode.
- Trading: orders, fills, positions, simulation/LIVE mode status.
- Accounts: default simulation account, LIVE account mapping, snapshots, cash, exposure, and P&L.
- Risk & Gateway: emergency stop, risk settings, selected broker adapter health, LIVE enablement.
- Notifications: email/Enterprise WeChat channels, test action, and delivery history.

### Data Flow

1. Providers sync stock master, corporate events, daily/minute bars, real-time quotes, and freshness state.
2. Administrator configures watchlist and strategy parameters.
3. Backtest engine or manual/trading-day scheduler invokes a validated strategy and emits normalized signals.
4. Risk engine evaluates SIMULATION/LIVE signals and creates order records only when allowed.
5. BrokerAdapter routes allowed orders to the simulation account or selected LIVE broker account.
6. Orders, fills, positions, account snapshots, ledger entries, risk events, gateway events, and notification events are persisted.

## Strategy Requirements

### Built-in "一夜持股法"

- Configurable entry window, defaulting to late-session evaluation.
- Filters for liquidity, market condition, event risk, stock status, and maximum position size.
- Generates entry signals for qualified stocks and exit intent for the next trading session.
- Must support simulation before LIVE activation.
- Must record why candidates are accepted or rejected.
- Defaults are defined in `spec.md` and remain editable per StrategyConfig.
- The strategy declares required timeframe `1m`; incompatible daily-only runs are rejected.
- Event-risk filtering uses normalized StockEvent records and rejects candidates when event data is missing or stale.

## Backtest Requirements

- Use Backtrader as the event-driven execution engine and extend it for A-share T+1, 100-share lots, suspension, and price-limit behavior.
- Daily and minute backtests use versioned data artifacts and immutable strategy/parameter snapshots.
- Backtests apply the configured CNY 10,000 default capital, commission, minimum commission, stamp tax, transfer fee, and slippage.
- Store aggregate metrics and trades in MySQL; store minute data and equity curves as Parquet artifacts.
- Prevent look-ahead access and record provider, timeframe, adjustment mode, date range, benchmark, and strategy version.

## Scheduling & Real-Time Requirements

- Manual strategy runs are always available to the administrator when market/risk prerequisites pass.
- Schedules are disabled by default, use the exchange calendar in `Asia/Shanghai`, and skip missed windows.
- Schedules declare entry_evaluation, exit_evaluation, or custom trigger; the overnight defaults are 14:40 entry evaluation and next-trading-day 09:35 exit evaluation.
- A unique schedule-window claim prevents duplicate execution after restart or concurrent workers.
- Real-time quote polling defaults to 5 seconds with a 15-second stale threshold; stale data blocks automated submission.

## Account Requirements

- v1 uses one default simulation account created automatically on first SIMULATION use.
- The simulation account stores configurable initial cash, cash balances, virtual P&L, fee rates, and slippage settings.
- Simulation fills must update cash, available cash, frozen cash, positions, account snapshots, and append-only ledger records.
- LIVE accounts are real broker accounts exposed by QMT, PTrade, or Futu OpenD adapters and stored locally as masked mappings only.
- Disabled or read-only LIVE account mappings must reject LIVE orders before broker submission.

### Python Plugin Strategies

- Plugins must declare strategy name, version, description, market, parameter schema, and signal output format.
- Plugins must run through validation before activation.
- Invalid plugins are visible in the UI but cannot be started.
- Plugin-generated signals must pass the same risk and broker path as built-in strategies.
- Only the administrator may register or execute plugins, and v1 loads them only from a trusted local directory in an isolated worker process.

## Deployment Plan

- macOS and Windows run backend, frontend, worker, and MySQL locally or through Docker Desktop/Compose.
- Linux deployment uses Docker Compose to run FastAPI, Vue/Nginx, MySQL, and background workers.
- QMT/miniQMT remains a remote Windows adapter; PTrade runs in the broker environment; Futu OpenD can run on macOS, Windows, or Linux where account permissions support the target securities.
- When the selected broker adapter is absent or unhealthy, LIVE mode remains unavailable while backtest and SIMULATION remain available.

## Complexity Tracking

No constitutional violations are accepted. Backtesting, scheduling, multiple broker adapters, and two notification channels increase v1 scope; each is retained because the user explicitly requires it. Adapter and provider abstractions isolate this complexity.

## Project Structure

```text
backend/
  app/
    auth/
    backtests/
    brokers/
    market_data/
    notifications/
    risk/
    schedules/
    strategies/
    stocks/
    watchlist/
frontend/
  src/
docker-compose.yml
specs/001-gupiao/
  contracts/openapi.yaml
  data-model.md
  plan.md
  quickstart.md
  research.md
  spec.md
  tasks.md
```

## Phase Gates

- **Phase 1**: Project skeleton, database, auth, stock master, watchlist.
- **Phase 2**: Market data adapters, strategy registry, built-in overnight strategy.
- **Phase 3**: Simulation account, simulation broker, risk engine, append-only trading records.
- **Phase 4**: Backtesting, minute-data cache, manual/trading-day scheduler.
- **Phase 5**: Cross-platform broker adapters, LIVE fail-closed behavior, admin controls.
- **Phase 6**: Email/Enterprise WeChat notifications, cross-platform packaging, deployment, and end-to-end tests.
