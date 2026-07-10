# Feature Specification: GuPiao Quant Trading Console

**Feature Branch**: `001-gupiao`  
**Created**: 2026-06-22  
**Status**: Draft  
**Input**: Build GuPiao, an A-share quantitative trading project using SpecKit and Python. It must support watchlists, stock name/code search, simulation/LIVE switching, selectable and custom strategies, and "一夜持股法" as one built-in strategy.

## User Scenarios & Testing

### Primary User Story

As a single administrator, I want to run a LAN-accessible web console for A-share quantitative trading so that I can search and follow stocks, configure strategies, validate them in simulation, and safely enable small-size automated LIVE trading through a broker gateway.

### Acceptance Scenarios

1. **Given** the administrator is logged in, **When** they search by stock code, Chinese name, or pinyin initials, **Then** matching A-share stocks are shown and can be added to the special watchlist without duplicates.
2. **Given** the administrator has created a strategy configuration, **When** they choose SIMULATION mode and start a run, **Then** the system generates signals, applies risk controls, creates simulated orders, and records run logs.
3. **Given** LIVE mode is disabled, **When** any strategy attempts to place a LIVE order, **Then** the order is rejected and a risk event is recorded.
4. **Given** LIVE mode is enabled and the selected broker adapter is healthy, **When** a strategy emits an order within configured limits, **Then** the order passes the risk gate and is sent through the BrokerAdapter.
5. **Given** the selected broker adapter is missing, unhealthy, or unreachable, **When** LIVE trading is requested, **Then** the system fails closed and no order is sent.
6. **Given** "一夜持股法" is selected, **When** its configured entry window and filters are satisfied, **Then** it can produce a next-session exit plan and observable strategy logs.
7. **Given** a Python plugin strategy is uploaded or enabled, **When** its metadata or parameter schema is invalid, **Then** it cannot be activated.
8. **Given** the administrator opens SIMULATION mode for the first time, **When** no simulation account exists, **Then** the system creates one default simulation account with configurable initial cash and records the initialization ledger entry.
9. **Given** a LIVE broker adapter is configured and healthy, **When** the administrator syncs LIVE accounts, **Then** GuPiao stores only the broker name, gateway association, masked account number, and enabled/read-only status.
10. **Given** a SIMULATION order is filled, **When** account balances are updated, **Then** commission, stamp tax, transfer fee, and configured slippage are reflected in cash, position cost, and account ledger records.
11. **Given** a strategy configuration and historical data range, **When** the administrator starts a backtest, **Then** the system applies A-share trading rules and costs and returns trades, equity curve, cumulative/annualized return, maximum drawdown, Sharpe ratio, win rate, profit factor, and turnover.
12. **Given** a strategy schedule is enabled, **When** its run time occurs on an exchange trading day, **Then** the strategy runs once; on a non-trading day or missed execution window it is skipped and logged.
13. **Given** the selected broker adapter is available on the current platform, **When** the administrator connects an eligible account, **Then** account synchronization and LIVE order routing use that adapter without requiring the GuPiao core to run on Windows.
14. **Given** a configured notification event occurs, **When** email and Enterprise WeChat channels are enabled, **Then** delivery attempts and results are recorded for both channels.
15. **Given** "一夜持股法" is configured, **When** schedules are created, **Then** separate entry-evaluation and next-session exit-evaluation triggers are available and each executes at most once per trading window.
16. **Given** a strategy requires minute data, **When** the administrator requests a daily-data backtest or critical corporate-event data is stale, **Then** execution is rejected with an actionable validation reason.

### Edge Cases

- Market data source A is unavailable while source B is available.
- Tushare token is not configured.
- A followed stock is suspended, ST, delisted, or has missing market data.
- The administrator switches from SIMULATION to LIVE while a strategy is already running.
- The emergency stop is triggered during an active strategy run.
- A strategy emits duplicate or conflicting orders for the same stock.
- LIVE mode is enabled but the administrator session expires.
- A simulation account is reset while strategies or orders are active.
- A LIVE account is present through the gateway but is disabled or read-only in GuPiao.
- Simulation cost settings are missing or invalid.
- Minute or real-time quote data is stale during the configured entry window.
- A scheduled run was missed because the service was offline.
- A backtest contains missing minute bars, suspended symbols, or price-limit sessions.
- An email or Enterprise WeChat notification fails while trading continues.
- Entry and exit schedules resolve to the same timestamp or overlap after an administrator edit.
- Corporate-event data is missing before a tail-session candidate evaluation.

## Requirements

### Functional Requirements

- **FR-001**: The system MUST provide a web console for a single administrator on a LAN.
- **FR-002**: The system MUST require administrator login before accessing trading, strategy, watchlist, or configuration features.
- **FR-003**: The system MUST provide a special watchlist for closely followed stocks.
- **FR-004**: The system MUST support stock search by A-share code, Chinese name, and pinyin initials.
- **FR-005**: The watchlist MUST prevent duplicate stocks and support add, remove, list, and quote refresh actions.
- **FR-006**: The system MUST support SIMULATION and LIVE trading modes and show the active mode clearly.
- **FR-007**: LIVE mode MUST be disabled by default.
- **FR-008**: Strategy execution MUST use the same signal, risk, order, and logging path for SIMULATION and LIVE modes.
- **FR-009**: The system MUST include a selectable built-in "一夜持股法" strategy.
- **FR-010**: The system MUST support built-in parameterized strategies configurable from the UI.
- **FR-011**: The system MUST support custom Python plugin strategies with declared metadata and parameter schema.
- **FR-012**: Plugin strategies MUST be disabled if validation fails.
- **FR-013**: All orders MUST pass risk checks before reaching a BrokerAdapter.
- **FR-014**: Risk checks MUST include per-order notional limit, maximum position exposure, daily loss circuit breaker, consecutive error pause, gateway health check, and emergency stop.
- **FR-015**: The system MUST record strategy runs, orders, fills, positions, risk events, and gateway events.
- **FR-016**: Trading records MUST be append-only from the application perspective.
- **FR-017**: The system MUST support AKShare and Tushare as switchable market data sources.
- **FR-018**: The system MUST continue operating in degraded mode when one configured data source is unavailable and another is healthy.
- **FR-019**: The system MUST provide a simulated broker for local development and validation.
- **FR-020**: The system MUST define a QMT/miniQMT remote gateway integration for LIVE A-share trading.
- **FR-021**: macOS, Windows, and Linux core deployments MUST NOT require QMT/miniQMT to be installed locally; QMT is an optional Windows broker adapter.
- **FR-022**: If the selected LIVE broker adapter is not configured, entitled, fresh, or healthy, affected LIVE orders MUST be rejected.
- **FR-023**: The system MUST expose order, fill, position, strategy, risk, and gateway status pages in the web console.
- **FR-024**: The system MUST support Linux Docker Compose deployment for the core application.
- **FR-025**: The system MUST create one default simulation account automatically on first SIMULATION use when no simulation account exists.
- **FR-026**: The default simulation account MUST have configurable initial cash and MUST record cash changes through an account ledger.
- **FR-027**: The simulated broker MUST calculate commission, stamp tax, transfer fee, and configurable slippage for simulated fills.
- **FR-028**: Simulation account reset MUST require administrator authorization and MUST be blocked while active strategy runs or open orders exist.
- **FR-029**: LIVE trading accounts MUST come from the configured broker gateway or administrator mapping, not from GuPiao account creation.
- **FR-030**: GuPiao MUST store only masked LIVE account identifiers and gateway association metadata, not broker login passwords.
- **FR-031**: A LIVE account disabled or marked read-only in GuPiao MUST NOT accept LIVE orders.
- **FR-032**: The system MUST store account snapshots for SIMULATION and LIVE modes to support account, cash, exposure, and P&L views.
- **FR-033**: The system MUST support historical backtests for built-in and validated plugin strategies using daily or minute data.
- **FR-034**: Backtests MUST apply A-share T+1, lot size, suspension, price-limit, commission, minimum commission, stamp tax, transfer fee, and slippage rules.
- **FR-035**: Backtest results MUST include trades, equity curve, cumulative return, annualized return, benchmark return, maximum drawdown, Sharpe ratio, win rate, profit factor, average win/loss, turnover, and exposure.
- **FR-036**: The system MUST prevent look-ahead data use and record the data source, adjustment mode, timeframe, date range, strategy version, and parameters for every backtest.
- **FR-037**: The system MUST support manual strategy runs and administrator-configured runs restricted to exchange trading days in the Asia/Shanghai timezone.
- **FR-038**: Scheduled runs MUST be disabled by default, execute at most once per scheduled window, and skip missed windows instead of placing late orders.
- **FR-039**: Market data providers MUST expose minute bars and tail-session real-time quotes; stale critical data MUST block automated orders.
- **FR-040**: The built-in "一夜持股法" MUST provide safe, configurable defaults for universe, entry window, filters, position sizing, and next-session exit.
- **FR-041**: SIMULATION MUST default to CNY 10,000 initial cash, 0.03% commission, CNY 5 minimum commission, 0.05% sell-side stamp tax, 0 transfer fee, and 5 bps slippage; every value MUST be configurable.
- **FR-042**: Risk settings MUST provide separate configurable SIMULATION and LIVE default profiles.
- **FR-043**: The GuPiao core MUST support macOS, Windows, and Linux independently of broker adapter platform requirements.
- **FR-044**: LIVE broker integrations MUST be selected through BrokerAdapter and may include remote QMT, broker-hosted PTrade, and Futu OpenD where account permissions support the target securities.
- **FR-045**: Only the administrator MAY register, enable, disable, or execute Python plugins; v1 MUST load plugins only from a trusted local directory and MUST NOT accept arbitrary web uploads.
- **FR-046**: The system MUST support SMTP email and Enterprise WeChat webhook notification channels.
- **FR-047**: Notifications MUST cover order success/failure, risk blocks and circuit breakers, gateway offline/recovery, strategy failure, and daily summary without blocking trading when delivery fails.
- **FR-048**: Every strategy definition MUST declare required data timeframes; "一夜持股法" requires `1m` data for backtest, SIMULATION, and LIVE evaluation, and incompatible runs MUST be rejected.
- **FR-049**: Tail-session real-time quotes MUST poll every 5 seconds by default and become stale after 15 seconds by default; both values MUST be configurable and stale quotes MUST block automated orders.
- **FR-050**: A CorporateEventProvider MUST supply suspension/resumption, regulatory investigation, material litigation, shareholder reduction, unlock, earnings-warning, and scheduled major-announcement events; providers sync every 300 seconds and become stale after 1,800 seconds by default, both configurable, and missing stale data MUST reject the candidate when event-risk filtering is enabled.
- **FR-051**: Strategy schedules MUST declare `entry_evaluation`, `exit_evaluation`, or `custom` trigger type; the built-in overnight strategy defaults to entry evaluation at `14:40` and exit evaluation at `09:35` on the next trading day.

### Non-Functional Requirements

- **NFR-001**: The UI MUST be in Chinese by default.
- **NFR-002**: The system MUST be usable on macOS and Windows for local development and simulation.
- **NFR-003**: The system MUST run the core services on Linux using Docker Compose.
- **NFR-004**: The system MUST fail closed for LIVE trading whenever authentication, risk, data, or gateway state is uncertain.
- **NFR-005**: Sensitive values such as administrator password hash, Tushare token, and gateway credentials MUST NOT be hard-coded.

## Key Entities

- **Administrator**: The single user authorized to manage strategies and trading modes.
- **Stock**: A-share security with code, exchange, Chinese name, pinyin metadata, status, and quote fields.
- **WatchlistItem**: A stock marked as specially followed by the administrator.
- **StrategyDefinition**: Built-in or plugin strategy metadata.
- **StrategyConfig**: User-configured strategy parameters and execution mode.
- **StrategyRun**: One execution instance with signals, state, and logs.
- **Order**: Requested trade after strategy signal and risk evaluation.
- **Fill**: Executed or simulated transaction record.
- **Position**: Current holding in SIMULATION or LIVE mode.
- **SimulationAccount**: Default virtual trading account with initial cash, balances, and simulated P&L.
- **SimulationAccountLedger**: Append-only cash and cost ledger for the simulation account.
- **LiveTradingAccount**: Local masked mapping of a real broker account exposed through a trading gateway.
- **AccountSnapshot**: Point-in-time account cash, asset, exposure, and P&L state.
- **StrategySchedule**: Trading-day-only schedule for a strategy configuration.
- **BacktestRun**: Reproducible historical strategy execution and aggregate metrics.
- **BacktestTrade**: Individual simulated trade generated by a backtest.
- **NotificationChannel**: Administrator-configured email or Enterprise WeChat destination.
- **NotificationDelivery**: Append-only notification attempt and result.
- **StockEvent**: Normalized corporate announcement/event used by strategy event-risk filtering and audit.
- **RiskEvent**: Record of risk checks, blocks, reductions, pauses, and emergency stops.
- **BrokerGateway**: External trading gateway health and configuration record.

## Success Criteria

- **SC-001**: An administrator can add a stock to the special watchlist by code, Chinese name, or pinyin initials in under 10 seconds.
- **SC-002**: A strategy run in SIMULATION mode can produce orders and logs without any LIVE gateway configured.
- **SC-003**: LIVE order attempts are blocked when LIVE mode is disabled, risk limits are breached, or the gateway is unhealthy.
- **SC-004**: "一夜持股法" appears as a selectable built-in strategy with configurable parameters.
- **SC-005**: Linux Docker Compose starts all broker-independent core services successfully.
- **SC-006**: First SIMULATION use creates a default simulation account and ledger entry without administrator database work.
- **SC-007**: Simulated fills update cash, positions, fees, slippage, and account snapshots.
- **SC-008**: LIVE account identifiers are masked in storage and UI, and disabled/read-only LIVE accounts cannot place orders.
- **SC-009**: An administrator can run a reproducible minute-data backtest and inspect metrics, trades, and equity curve.
- **SC-010**: A scheduled strategy executes only once in its configured window on an exchange trading day and never catches up outside that window.
- **SC-011**: The core application and simulation workflow run on macOS and Windows, while Linux Docker Compose remains supported for deployment.
- **SC-012**: Email and Enterprise WeChat delivery results are visible for every configured notification event.
- **SC-013**: Incompatible timeframe requests and stale minute/real-time/event data are rejected before strategy execution or order creation.
- **SC-014**: Overnight entry and exit schedules execute independently and at most once in their valid trading windows.

## Clarifications

- **2026-06-22**: v1 uses one default simulation account created automatically on first SIMULATION use.
- **2026-06-22**: LIVE accounts are real broker accounts exposed by the selected BrokerAdapter and stored locally only as masked mappings.
- **2026-06-22**: SIMULATION fills include configurable commission, stamp tax, transfer fee, and slippage.
- **2026-06-22**: v1 includes historical backtesting, manual runs, and trading-day scheduled runs.
- **2026-06-22**: SIMULATION defaults to CNY 10,000, 0.03% commission, CNY 5 minimum commission, 0.05% sell-side stamp tax, 0 transfer fee, and 5 bps slippage.
- **2026-06-22**: The core is cross-platform; LIVE trading uses pluggable broker adapters rather than requiring Windows-only deployment.
- **2026-06-22**: Only the administrator can use trusted local Python plugins.
- **2026-06-22**: Notifications use SMTP email and Enterprise WeChat webhooks.
- **2026-06-22**: "一夜持股法" requires minute data and uses separate entry and next-session exit schedule triggers.
- **2026-06-22**: Real-time quotes poll every 5 seconds and become stale after 15 seconds by default.
- **2026-06-22**: Corporate-event data comes from normalized CNINFO/Tushare/AKShare providers; missing fresh event data rejects a candidate when event filtering is enabled.
- **2026-06-22**: Corporate-event synchronization defaults to every 300 seconds and is stale after 1,800 seconds.

## Built-in Strategy Defaults

The following "一夜持股法" defaults are starting assumptions for backtest and SIMULATION and are configurable:

- Timezone: `Asia/Shanghai`.
- Required timeframe: `1m`.
- Universe: SSE and SZSE main board, STAR Market, and ChiNext; BSE disabled by default.
- Exclusions: ST/*ST, suspended/delisting, fewer than 60 trading days listed, or not tradable at the price limit.
- Evaluation time: `14:40`; entry window: `14:45-14:55`.
- Liquidity: daily turnover amount at least CNY 100 million and turnover rate at least 1%.
- Momentum: intraday return between 1% and 5%, price above intraday VWAP and 5-session moving average.
- Market filter: benchmark `000300.SH` at evaluation is not below its 5-session moving average.
- Event risk: enabled; reject suspension/resumption, regulatory investigation, material litigation, shareholder reduction, unlock above 5% of free float, earnings warning, or a scheduled major announcement before the next-session exit. Missing or stale event data also rejects the candidate.
- Selection: at most 3 candidates, target 20% of account equity per candidate, always capped by risk settings.
- Exit evaluation: next trading day at `09:35`; forced exit at `09:45`; latest exit `10:00`.
- Gap handling: exit at the first tradable opportunity when opening gap is at least +1.5% or at most -2.0%.

## Default Risk Profiles

All defaults are configurable and the lower of percentage and absolute limits applies where both are set.

- SIMULATION: maximum order 20% of equity/CNY 2,000; maximum single position 20%; maximum total exposure 60%; daily loss circuit breaker 3%; maximum 3 consecutive execution errors.
- LIVE: maximum order 5% of equity/CNY 5,000; maximum single position 10%; maximum total exposure 30%; daily loss circuit breaker 1%; maximum 3 consecutive execution errors; maximum 5 submitted orders per trading day.
