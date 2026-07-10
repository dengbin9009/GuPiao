# Requirements Checklist: GuPiao

**Feature**: `001-gupiao`  
**Created**: 2026-06-22  
**Reviewed Artifacts**: `spec.md`, `plan.md`, `data-model.md`, `contracts/openapi.yaml`, `tasks.md`, `quickstart.md`, `research.md`, `.specify/memory/constitution.md`

## Summary

- Total checks: 68
- Passed: 68
- Failed: 0
- Blocking issues: 0

## Requirement Quality

- [x] Requirements describe user-visible behavior, not only implementation details.
- [x] Requirements use testable MUST statements for critical behavior.
- [x] Acceptance scenarios cover the main administrator workflow.
- [x] Success criteria include watchlist, simulation, LIVE blocking, built-in strategy, and deployment outcomes.
- [x] Edge cases cover data source failures, session expiry, active runs, gateway failures, and account reset constraints.
- [x] Clarifications are recorded for accounts, costs, backtesting, scheduling, strategy defaults, cross-platform brokers, plugins, and notifications.

## Scope & Consistency

- [x] v1 market scope is clearly limited to A-shares.
- [x] UI language is clearly set to Chinese by default.
- [x] Product shape is consistently a LAN-accessible Web console.
- [x] Technology stack is consistent across plan and quickstart: FastAPI, Vue, MySQL, Docker Compose.
- [x] macOS, Windows, and Linux support boundaries are clear.
- [x] QMT, PTrade, and Futu OpenD platform/account constraints are explicit.

## Simulation Mode

- [x] SIMULATION and LIVE modes are explicitly distinct.
- [x] LIVE trading is disabled by default.
- [x] Simulation can run without any LIVE gateway.
- [x] A default simulation account is created automatically on first SIMULATION use.
- [x] Simulation account initial cash is configurable.
- [x] Simulation initial cash defaults to CNY 10,000.
- [x] Simulation ledger entries are append-only.
- [x] Simulated fills include commission, minimum commission, stamp tax, transfer fee, and slippage.
- [x] Simulation account reset is blocked while active runs or open orders exist.

## LIVE Mode & Account Safety

- [x] LIVE accounts come from broker gateway synchronization or administrator mapping, not from GuPiao account creation.
- [x] LIVE account identifiers are masked in local storage and UI.
- [x] GuPiao does not store broker login passwords.
- [x] Disabled or read-only LIVE accounts block order submission.
- [x] Gateway missing, unhealthy, unreachable, or unauthorized states block LIVE orders.
- [x] LIVE orders must pass authentication, account, gateway, and risk checks.
- [x] Separate configurable SIMULATION and LIVE risk profiles have explicit conservative defaults.

## Strategy Requirements

- [x] "一夜持股法" is a selectable built-in strategy, not the only strategy.
- [x] Built-in strategies support UI parameter configuration.
- [x] Python plugin strategies require metadata and parameter schema validation.
- [x] Invalid plugin strategies cannot be activated.
- [x] Only the administrator may register or execute plugins from the trusted local directory.
- [x] Strategy signals use the same risk and broker path in SIMULATION and LIVE modes.
- [x] "一夜持股法" entry, filters, sizing, and next-session exit defaults are explicit and configurable.

## Backtesting

- [x] Built-in and validated plugin strategies support daily and minute historical backtests.
- [x] Backtests apply A-share T+1, lot size, suspension, and price-limit behavior.
- [x] Commission, minimum commission, stamp tax, transfer fee, and slippage are included.
- [x] Backtest runs capture immutable strategy, parameter, provider, timeframe, date-range, adjustment, and cost metadata.
- [x] Required return, drawdown, risk, trade, turnover, and exposure metrics are explicit.
- [x] Look-ahead prevention and reproducibility tests are included.
- [x] MySQL/Parquet storage boundaries are explicit.

## Manual Runs, Scheduling, and Real-Time Data

- [x] Administrator manual runs are supported.
- [x] Schedules are disabled by default and restricted to exchange trading days in Asia/Shanghai.
- [x] Missed schedule windows are skipped rather than replayed.
- [x] Idempotent schedule-window claims prevent duplicate execution.
- [x] Minute bars and tail-session real-time quotes are required capabilities.
- [x] Stale critical quotes block automated order creation.
- [x] Strategy definitions declare required timeframes and incompatible runs are rejected.
- [x] "一夜持股法" explicitly requires `1m` data.
- [x] Real-time polling defaults to 5 seconds and quote staleness defaults to 15 seconds.
- [x] Strategy schedules distinguish entry_evaluation, exit_evaluation, and custom triggers.
- [x] Overnight entry and next-session exit trigger times are explicit.

## Corporate Event Risk

- [x] CNINFO/Tushare/AKShare corporate-event sources are normalized into StockEvent records.
- [x] Corporate-event sync and stale thresholds are explicit and configurable.
- [x] Missing or stale event data rejects event-filtered candidates and is covered by tests/API/tasks.

## Cross-Platform Brokers and Notifications

- [x] Core application behavior is independent of broker adapter operating-system requirements.
- [x] QMT remains available through a remote Windows gateway.
- [x] PTrade is modeled as a broker-hosted adapter path.
- [x] Futu OpenD is modeled as a macOS/Windows/Linux path subject to account permissions.
- [x] Email and Enterprise WeChat channels are both required.
- [x] Required notification event types are explicit.
- [x] Notification secrets are referenced rather than logged or returned.
- [x] Delivery retry/failure is recorded and does not block trading.

## Data & API Coverage

- [x] Core entities are defined for administrator, stocks, watchlist, strategies, signals, orders, fills, positions, risk, and gateway.
- [x] Account entities are defined for simulation accounts, simulation ledgers, LIVE account mappings, and account snapshots.
- [x] OpenAPI includes stock search, watchlist, strategies, backtests, schedules, orders, risk, gateways, accounts, market data, and notifications.
- [x] Task list includes implementation work for all required feature areas and cross-platform deployment.

## Notes

- No blocking requirement ambiguity remains for v1 planning.
- Implementation may choose the physical database FK strategy for `account_id`; the specification treats `mode` as the discriminator between simulation and LIVE account contexts.
