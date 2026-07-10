# Analysis: GuPiao

**Feature**: `001-gupiao`  
**Created**: 2026-06-22  
**Analyzed Artifacts**: `spec.md`, `plan.md`, `data-model.md`, `contracts/openapi.yaml`, `tasks.md`, `quickstart.md`, `research.md`, `checklists/requirements.md`, `.specify/memory/constitution.md`

## Summary

- Requirements: 51 functional requirements, 5 non-functional requirements, 14 success criteria.
- Data model: 26 MySQL tables plus versioned Parquet market-data/equity artifacts.
- Tasks: 99 unique tasks, ordered across 14 implementation phases.
- API contract: 37 paths, 27 schemas.
- Blocking issues remaining: 0.
- Issues found during analysis: 6.
- Issues fixed during analysis: 6.

## Findings Fixed

### A-001: API contract did not cover all required console views

**Severity**: Medium  
**Status**: Fixed

The spec requires the web console to expose orders, fills, positions, strategy, risk, and gateway status pages. The previous OpenAPI contract covered orders, risk settings, and gateway health, but did not include fills, positions, strategy runs/logs, risk events, gateway events, or market data source state.

Fix applied:

- Added schemas: `Fill`, `Position`, `StrategyRun`, `StrategyLog`, `RiskEvent`, `GatewayEvent`, `DataSourceState`.
- Added endpoints: `/fills`, `/positions`, `/strategy-runs`, `/strategy-runs/{run_id}/logs`, `/risk/events`, `/gateway/events`, `/market-data/sources`.

### A-002: Task dependency order put broker implementation before broker interface

**Severity**: Medium  
**Status**: Fixed

The task list previously scheduled `SimulatedBrokerAdapter` before the unified `BrokerAdapter` interface and placed a full strategy-to-simulation integration test before the risk engine tasks. That ordering conflicted with the constitution requirement that all orders pass through the shared risk and broker path.

Fix applied:

- Moved unified `BrokerAdapter` interface work into Phase 7 before `SimulatedBrokerAdapter`.
- Reworded the simulation test task to cover account/broker behavior only.
- Moved the strategy-signal-through-risk-gate simulation test into the risk phase.
- Replaced the later duplicate broker-interface task with LIVE account target selection and BrokerAdapter handoff.

### A-003: Overnight scheduling did not distinguish entry and exit triggers

**Severity**: High  
**Status**: Fixed

The previous StrategySchedule model had only one `run_time`, leaving the implementer to decide how next-session exits were scheduled. It now requires `entry_evaluation`, `exit_evaluation`, or `custom` trigger type. The built-in overnight defaults are 14:40 entry evaluation and next-trading-day 09:35 exit evaluation, each protected by an idempotent schedule-window claim.

### A-004: Event-risk filtering lacked a normalized data source

**Severity**: High  
**Status**: Fixed

The overnight strategy required event-risk filtering but did not define announcement sources, persistence, freshness, or fail-closed behavior. The specification now defines CorporateEventProvider with CNINFO/Tushare/AKShare sources, StockEvent persistence/API, 300-second sync, 1,800-second stale threshold, and candidate rejection when required event data is unavailable.

### A-005: Intraday timeframe and real-time freshness defaults were incomplete

**Severity**: High  
**Status**: Fixed

The previous documents allowed daily or minute backtests without declaring which strategies require minute data, and specified a stale threshold without a polling cadence. StrategyDefinition now declares required timeframes; "一夜持股法" requires `1m`. Real-time quotes poll every 5 seconds and become stale after 15 seconds by default, with incompatible/stale runs blocked.

### A-006: Legacy QMT-only wording conflicted with cross-platform adapters

**Severity**: Medium  
**Status**: Fixed

Several older requirements still described macOS-only local development or QMT as the only LIVE gateway. They now consistently define macOS/Windows local support, Linux broker-independent deployment, and fail-closed behavior for the selected BrokerAdapter.

## Coverage Assessment

- **Watchlist and search**: Covered by requirements, data model, API, tasks, and UI tasks.
- **Strategy selection and custom strategy support**: Covered by strategy definition/config models, API, tasks, and plugin validation requirements.
- **"一夜持股法"**: Covered as a built-in parameterized strategy with candidate filtering, logging, simulation support, and tests.
- **Simulation account**: Covered by requirements, data model, API, quickstart, tasks, and checklist.
- **LIVE account mapping**: Covered by requirements, data model, API, quickstart, tasks, and fail-closed safety rules.
- **Risk controls**: Covered by constitution, requirements, data model, API, tasks, and negative tests.
- **Historical backtesting**: Covered by Backtrader plan, reproducibility metadata, Parquet artifacts, API, UI, metrics, and A-share execution tests.
- **Manual/scheduled runs**: Covered by exchange-calendar, skip-misfire, idempotency, real-time freshness, API, UI, and tests.
- **Cross-platform brokers**: Covered by adapter-neutral core plus QMT, PTrade, and Futu OpenD paths with permission checks.
- **Notifications**: Covered by SMTP email, Enterprise WeChat, delivery records, retry, UI/API, and non-blocking behavior.
- **Corporate event risk**: Covered by provider abstraction, StockEvent model/API, normalization/deduplication, freshness thresholds, fail-closed behavior, and strategy tests.
- **Linux Docker Compose deployment**: Covered by requirements, plan, quickstart, and tasks.

## Remaining Non-Blocking Notes

- The physical database implementation can choose either polymorphic `account_id` plus `mode`, or separate nullable account foreign keys. The current specification intentionally keeps this as an implementation detail while requiring `mode` to discriminate account context.
- QMT and PTrade gateway request/response details remain broker-specific and must be finalized against the selected broker's granted API environment.
- Futu OpenD is cross-platform, but tradable A-share coverage remains an account/region entitlement check rather than a GuPiao guarantee.
- BSE is disabled in the default "一夜持股法" universe but remains available as a configurable stock universe option.
- Actual CNINFO endpoint credentials/rate limits and broker-specific PTrade/QMT contracts remain deployment configuration rather than product ambiguity.

## Validation

- OpenAPI YAML parses successfully.
- Required account, trading, strategy, backtest, schedule, risk, gateway, market-data, and notification paths are present.
- Required timeframe, schedule-trigger, StockEvent, and freshness fields are present across spec, model, API, tasks, and quickstart.
- Required schemas are present.
- Task IDs are unique.
- No checklist failures remain.
