# Tasks: GuPiao Quant Trading Console

**Input**: `specs/001-gupiao/spec.md` and `specs/001-gupiao/plan.md`

## Phase 1: Project Foundation

- [x] T001 Initialize backend FastAPI project structure.
- [x] T002 Initialize Vue frontend project structure.
- [x] T003 Add Docker Compose services for backend, frontend/Nginx, MySQL, background worker, scheduler, and Parquet artifact volume.
- [x] T004 Add environment configuration for database, admin credentials, market data, broker adapters, trusted plugin directory, simulation defaults, risk profiles, email, and Enterprise WeChat.
- [x] T005 Add database migrations for all entities listed in `data-model.md`.

## Phase 2: Authentication and Admin Shell

- [x] T006 Implement single administrator login with password hash storage.
- [x] T007 Implement session timeout and protected API dependencies.
- [x] T008 Implement LAN/IP allowlist enforcement.
- [x] T009 Build Chinese admin layout with dashboard, navigation, and mode indicator.

## Phase 3: Stock Master, Search, and Watchlist

- [x] T010 Implement Stock and StockEvent entities plus stock master/event synchronization.
- [x] T011 Implement pinyin and pinyin-initial metadata generation for stock search.
- [x] T012 Implement stock search by code, Chinese name, and pinyin initials.
- [x] T013 Implement special watchlist add, remove, list, and duplicate prevention.
- [x] T014 Implement watchlist quote refresh and missing-data display states.
- [x] T015 Build frontend watchlist page with search and add/remove actions.

## Phase 4: Market Data Providers

- [x] T016 Implement MarketDataProvider and CorporateEventProvider interfaces with daily, minute, real-time, and event capability metadata.
- [x] T017 Implement AKShare provider for A-share stock master, quotes, daily/minute bars, and corporate-event fallback.
- [x] T018 Implement Tushare provider for stock master, quotes, daily/minute bars, exchange calendar, and corporate-event fallback.
- [x] T019 Implement CNINFO corporate-event provider, provider selection, 300-second sync, freshness checks, health checks, and degraded-source behavior.
- [x] T020 Add integration tests for market-data switching, event normalization/deduplication, and stale-event fail-closed behavior.

## Phase 5: Strategy System

- [x] T021 Implement StrategyDefinition/StrategyConfig persistence including required timeframes.
- [x] T022 Implement strategy registry for built-in and plugin strategies.
- [x] T023 Implement parameter schema validation for strategies.
- [x] T024 Implement administrator-only Python plugin scanning, loading, validation, and isolated execution from a trusted local directory.
- [x] T025 Build strategy center UI for selecting, configuring, enabling, and disabling strategies.
- [x] T026 Add tests for invalid plugin metadata, invalid parameters, and disabled strategies.

## Phase 6: Built-in "一夜持股法"

- [x] T027 Implement "一夜持股法" with the configurable defaults defined in `spec.md`.
- [x] T028 Add configurable universe, entry/exit window, liquidity, market, event-risk, listing-age, tradability, and position-size filters.
- [x] T029 Record candidate accept/reject reasons in strategy run logs.
- [x] T030 Generate entry signals plus separate next-session exit intent/trigger in SIMULATION mode.
- [x] T031 Add strategy tests for timeframe enforcement, event filtering, candidate filtering, position sizing, and exit intent.

## Phase 7: Simulation, Orders, and Records

- [x] T032 Implement normalized signal, order, fill, position, and strategy run models.
- [x] T033 Implement default SimulationAccount creation on first SIMULATION use.
- [x] T034 Implement SimulationAccountLedger for initialization, order freeze, fills, releases, fees, resets, and adjustments.
- [x] T035 Implement configurable simulated commission, stamp tax, transfer fee, and slippage.
- [x] T036 Implement unified BrokerAdapter interface.
- [x] T037 Implement SimulatedBrokerAdapter order placement, fills, positions, cash, P&L, and account snapshots.
- [x] T038 Block simulation account reset while active strategy runs or open orders exist.
- [x] T039 Enforce append-only behavior for trading records and simulation ledger records.
- [x] T040 Build simulation account, orders, fills, positions, and run history UI pages.
- [x] T041 Add simulation account and broker tests for ledger, fill, position, and snapshot updates.

## Phase 8: Risk Engine

- [x] T042 Implement separate configurable SIMULATION and LIVE risk profiles with the defaults defined in `spec.md`.
- [x] T043 Implement per-order notional limit and maximum exposure checks.
- [x] T044 Implement daily loss circuit breaker.
- [x] T045 Implement consecutive error pause.
- [x] T046 Implement emergency stop.
- [x] T047 Implement risk event logging.
- [x] T048 Add negative tests for blocked orders, fail-closed behavior, and strategy signal through risk gate to simulated broker.

## Phase 9: LIVE Mode and QMT Gateway Contract

- [x] T049 Implement LIVE account target selection and BrokerAdapter handoff.
- [x] T050 Implement LiveTradingAccount masked mapping synced from the selected broker adapter or administrator mapping.
- [x] T051 Implement QmtGatewayBrokerAdapter using the remote gateway contract.
- [x] T052 Implement broker gateway registry, capability/health checks, account sync, and gateway event logging.
- [x] T053 Implement LIVE mode enable/disable controls with administrator authorization.
- [x] T054 Ensure LIVE mode is disabled by default.
- [x] T055 Ensure disabled/read-only LIVE accounts cannot place orders.
- [x] T056 Ensure gateway missing/unhealthy state blocks all LIVE orders.
- [x] T057 Build risk, gateway, and LIVE account UI page.
- [x] T058 Add tests for LIVE disabled, read-only account, gateway unavailable, breached risk limits, and successful allowed LIVE order handoff.

## Phase 10: Historical Backtesting

- [x] T059 Integrate Backtrader behind a BacktestEngine interface.
- [x] T060 Implement A-share backtest rules for T+1, 100-share lots, suspensions, price limits, commission, minimum commission, stamp tax, transfer fee, and slippage.
- [x] T061 Implement versioned Parquet storage for daily/minute bars and equity curves.
- [x] T062 Implement BacktestRun and BacktestTrade persistence with immutable strategy, parameter, data-provider, and cost snapshots.
- [x] T063 Implement daily/minute backtest execution for built-in and validated plugin strategies.
- [x] T064 Calculate cumulative/annualized/benchmark return, maximum drawdown, Sharpe ratio, win rate, profit factor, average win/loss, turnover, and exposure.
- [x] T065 Implement backtest APIs for create, list, detail, trades, status, and artifact retrieval.
- [x] T066 Build backtest UI with parameter form, run status, metric table, equity curve, and trade list.
- [x] T067 Add tests for look-ahead prevention, reproducibility, missing bars, suspension, price limits, T+1, and cost calculations.
- [x] T068 Add "一夜持股法" minute-data backtest acceptance test using CNY 10,000 default capital.

## Phase 11: Manual Runs, Trading-Day Scheduling, and Real-Time Data

- [x] T069 Implement SSE/SZSE/BSE trading calendar service in `Asia/Shanghai` with provider fallback.
- [x] T070 Implement StrategySchedule trigger types, disabled-by-default behavior, overnight 14:40/09:35 defaults, and `skip` misfire policy.
- [x] T071 Implement idempotent schedule-window claims to prevent duplicate runs.
- [x] T072 Implement administrator manual strategy run action with market, quote-freshness, account, and risk prechecks.
- [x] T073 Implement entry/exit/custom scheduled worker execution restricted to valid exchange trading windows.
- [x] T074 Implement minute-bar retrieval and Parquet cache refresh.
- [x] T075 Implement 5-second default tail-session quote polling with configurable 15-second stale threshold.
- [x] T076 Block automated order creation when critical quotes are stale or missing.
- [x] T077 Build schedule, manual-run, market-data health, and quote-freshness UI controls.
- [x] T078 Add tests for entry/exit triggers, holidays, missed windows, restart idempotency, duplicate workers, incompatible timeframes, and stale real-time quotes.

## Phase 12: Cross-Platform LIVE Broker Adapters

- [x] T079 Define and document the broker capability matrix for simulation, QMT, PTrade, and Futu OpenD.
- [x] T080 Implement PTradeBrokerAdapter contract and broker-hosted gateway integration path.
- [x] T081 Implement FutuOpenDBrokerAdapter for macOS, Windows, and Linux where account permissions support target securities.
- [x] T082 Validate LIVE account market permissions and symbol eligibility before order creation.
- [x] T083 Extend gateway and account UI for adapter type, platform, capabilities, health, and permission status.
- [x] T084 Add mocked contract tests for QMT, PTrade, and Futu OpenD account/order/fill/position normalization.
- [x] T085 Add cross-platform smoke tests for macOS and Windows simulation plus adapter startup/health checks.

## Phase 13: Email and Enterprise WeChat Notifications

- [x] T086 Implement NotificationChannel and NotificationDelivery persistence with secret references.
- [x] T087 Implement SMTP email delivery.
- [x] T088 Implement Enterprise WeChat webhook delivery.
- [x] T089 Implement notification routing for order success/failure, risk block/circuit breaker, gateway offline/recovery, strategy failure, and daily summary.
- [x] T090 Implement up to three asynchronous delivery attempts without blocking trading.
- [x] T091 Build notification channel configuration, test-send, and delivery-history UI.
- [x] T092 Add tests for redaction, successful delivery, retry, final failure, and non-blocking trading behavior.

## Phase 14: Deployment and Documentation

- [x] T093 Document macOS and Windows local development/runtime quickstarts.
- [x] T094 Document Linux Docker Compose deployment and Parquet artifact backups.
- [x] T095 Document environment variables, secret references, trusted plugin directory, and notification settings.
- [x] T096 Document simulation defaults, risk profiles, reset behavior, fees, and slippage.
- [x] T097 Document QMT, PTrade, and Futu OpenD adapter setup, platform constraints, and account-permission limits.
- [x] T098 Document backtesting, trading-day schedules, minute/real-time data freshness, email, and Enterprise WeChat.
- [x] T099 Add end-to-end smoke tests for login, watchlist, backtest, manual/scheduled simulation, account updates, notifications, cross-platform startup, and LIVE fail-closed behavior.
