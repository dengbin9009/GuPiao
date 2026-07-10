# GuPiao Constitution

## Core Principles

### I. Real-Money Safety First
GuPiao MUST treat real-money trading as the highest-risk capability. LIVE trading is disabled by default, requires administrator authentication, and MUST pass all configured risk controls before any order leaves the system. If a trading gateway, market data source, risk rule, or broker adapter is unavailable or ambiguous, the system MUST fail closed and reject LIVE orders.

### II. Simulation Before LIVE
Every strategy MUST be runnable in SIMULATION mode before it can be enabled in LIVE mode. SIMULATION and LIVE orders MUST use the same strategy signal and risk-control path, with only the BrokerAdapter implementation changing. Strategy behavior that cannot be observed in simulation MUST NOT be enabled for automated LIVE trading.

### III. Mandatory Risk Gate
All automated orders MUST pass a risk gate that enforces at least: per-order notional limit, maximum position exposure, daily loss circuit breaker, consecutive error pause, gateway health check, and emergency stop. Risk events MUST be recorded whenever an order is blocked, reduced, paused, or rejected.

### IV. Immutable Trading Records
Orders, fills, positions, strategy runs, risk events, and gateway events MUST be append-only from the application perspective. Correction records may be added, but historical trading records MUST NOT be deleted or silently overwritten.

### V. Portable Core, External Broker Gateways
The GuPiao core application MUST run on macOS and Windows for local use and Linux with Docker Compose for deployment. No single broker runtime may be a mandatory core dependency. LIVE execution MUST use BrokerAdapter implementations such as remote QMT, broker-hosted PTrade, or cross-platform Futu OpenD, subject to the connected account's market permissions.

## Product Constraints

- v1 targets A-share quantitative stock trading with a Chinese user interface.
- v1 is a single-administrator web console intended for LAN access.
- v1 supports both built-in parameterized strategies and custom Python plugin strategies.
- The built-in strategy catalog MUST include "一夜持股法" as one selectable strategy, not as the only supported strategy.
- v1 MUST support historical backtesting before a strategy is enabled for automated LIVE trading.
- The system MUST clearly distinguish SIMULATION and LIVE modes in the UI, API, logs, and persisted records.
- LIVE automated trading defaults MUST be conservative and explicitly configurable.
- Python plugin registration and execution MUST be restricted to the administrator and trusted local plugin directory.

## Technology Constraints

- Backend: Python with FastAPI.
- Frontend: Vue.
- Database: MySQL.
- Market data: AKShare and Tushare as switchable data sources.
- Backtesting: Backtrader with GuPiao A-share execution-rule extensions.
- Deployment: Docker Compose for Linux production deployment and local development services.
- Broker access: unified BrokerAdapter interface, with simulation, remote QMT, PTrade, and Futu OpenD adapter paths.
- Notifications: SMTP email and Enterprise WeChat webhook channels.

## Development Workflow

- Specifications MUST be created before implementation tasks.
- Tests MUST cover strategy logic, risk gates, broker adapters, and LIVE fail-closed behavior.
- Backtests MUST prevent look-ahead behavior and apply A-share T+1, transaction-cost, slippage, and price-limit assumptions.
- LIVE-related code MUST include negative tests for missing authentication, disabled LIVE mode, gateway unavailability, and breached risk limits.
- New strategies MUST include parameter validation, simulation support, and observable run logs.

## Governance

This constitution overrides conflicting implementation choices. Changes require updating this file, recording the rationale in the active feature plan, and revising affected tasks before implementation continues.

**Version**: 1.1.0  
**Ratified**: 2026-06-22  
**Last Amended**: 2026-06-22
