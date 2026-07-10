# Research: GuPiao

## Decision: FastAPI + Vue

**Decision**: Use FastAPI for the backend and Vue for the web console.

**Rationale**: The project is Python-centered for quantitative trading, strategy execution, market data, and broker adapters. FastAPI keeps API development close to the trading engine while providing typed request/response validation. Vue gives enough structure for a richer admin console than a pure server-rendered UI.

**Alternatives considered**:

- CLI-only: faster to start but unsuitable for watchlist, mode switching, risk controls, and monitoring.
- Streamlit: fast for prototypes but weaker for durable admin workflows, API boundaries, and LIVE trading controls.
- FastAPI + HTMX: simpler than Vue but less suitable for strategy configuration and live console interactions.

## Decision: MySQL

**Decision**: Use MySQL as the persistent database.

**Rationale**: The user selected MySQL. It is suitable for watchlists, strategy configuration, trading logs, positions, and risk events. It also works cleanly in Docker Compose for Linux deployment.

**Alternatives considered**:

- SQLite: simpler for local use but less aligned with deployment and growth needs.
- PostgreSQL: strong default for many apps, but the user selected MySQL.

## Decision: AKShare + Tushare switchable data sources

**Decision**: Implement AKShare and Tushare behind a MarketDataProvider abstraction.

**Rationale**: AKShare is convenient for broad public data access and quick prototyping. Tushare provides more structured datasets when a token and permissions are available. A provider abstraction prevents strategies and UI flows from depending on one vendor's field names.

**Consequences**:

- Provider health must be visible.
- Field normalization is required before strategy use.
- Tushare token must be configured through environment variables.

## Decision: Cross-platform BrokerAdapter strategy

**Decision**: Keep the GuPiao core cross-platform and use pluggable simulation, remote QMT, PTrade, and Futu OpenD adapters.

**Rationale**: QMT is useful for mainland broker access but ties the broker runtime to Windows. PTrade can execute in a broker-hosted environment. Futu OpenD officially supports Windows, macOS, CentOS, and Ubuntu, but its A-share trading universe depends on account region and permissions. Therefore no single adapter can satisfy every account; the core must remain portable and adapter-neutral.

**Consequences**:

- SimulatedBrokerAdapter is required for v1.
- LIVE orders fail closed when adapter URL, credentials, account entitlement, quote freshness, health, or risk state is invalid.
- Gateway behavior must be tested through mocked HTTP responses.
- QMT is the recommended full mainland A-share route when a Windows broker host is available.
- Futu OpenD is the recommended native cross-platform route when the user's account can trade the target securities.
- PTrade remains a broker-dependent cloud alternative.

**Primary references**:

- [Futu OpenD overview](https://openapi.futunn.com/futu-api-doc/en/opend/opend-intro.html)
- [Futu OpenAPI market/account overview](https://openapi.futunn.com/futu-api-doc/en/intro/intro.html)
- [Shanxi Securities PTrade](https://www.i618.com.cn/main/companybusi/wealth/quantitativetrading/ptrade/index.shtml)

### Broker capability matrix

| Adapter | Core platforms | Broker runtime | Accounts | Orders | Positions/fills | A-share scope |
|---|---|---|---|---|---|---|
| Simulation | macOS, Windows, Linux | GuPiao process | Local virtual account | Full simulated lifecycle | Full local normalization | SSE, SZSE, BSE by strategy configuration |
| QMT | macOS, Windows, Linux | Remote Windows QMT host | Synced masked mappings | HTTP gateway | Gateway-normalized | Depends on mainland broker/QMT account permissions |
| PTrade | macOS, Windows, Linux | Broker-hosted environment | Synced masked mappings | HTTP gateway | Gateway-normalized | Depends on broker API entitlement |
| Futu OpenD | macOS, Windows, Linux | Local/remote OpenD | OpenD account query | Futu SDK | Futu SDK normalization | Depends on account region and market permissions |

GuPiao validates adapter health, account enabled/read-only state, market permission, symbol eligibility, quote freshness, and risk limits before LIVE order submission. Adapter availability never enables LIVE mode automatically.

## Decision: Backtrader for historical backtesting

**Decision**: Use Backtrader for event-driven historical backtests and add GuPiao-specific A-share execution rules.

**Rationale**: Backtrader provides an event-driven broker model with configurable commission and slippage. GuPiao still needs explicit T+1, 100-share lot, suspension, and price-limit extensions, but using a proven broker/execution engine avoids rebuilding basic order lifecycle logic.

**Primary references**:

- [Backtrader slippage](https://www.backtrader.com/docu/slippage/slippage/)
- [Backtrader commission schemes](https://www.backtrader.com/docu/commission-schemes/commission-schemes/)

## Decision: Parquet historical data artifacts

**Decision**: Store versioned daily/minute bars and equity curves as Parquet artifacts; keep run metadata, trades, and metrics in MySQL.

**Rationale**: Minute bars grow much faster than transactional application data. File-based columnar artifacts are better suited for backtest scans, while MySQL remains the source of truth for reproducibility metadata and user-facing records.

## Decision: Trading-day scheduler

**Decision**: Support administrator manual runs and persistent scheduled runs using Asia/Shanghai exchange calendars, disabled by default.

**Rationale**: Scheduled trading must never run on holidays or replay a missed tail-session order after the valid window. A unique schedule-window claim provides idempotency across worker restarts.

**Clarification**: StrategySchedule uses explicit entry_evaluation, exit_evaluation, and custom triggers. "一夜持股法" defaults to 14:40 entry evaluation and next-trading-day 09:35 exit evaluation.

## Decision: Corporate event risk data

**Decision**: Normalize corporate-event data from CNINFO first, then Tushare/AKShare fallbacks, into StockEvent records used by strategy filtering and audit.

**Rationale**: The overnight strategy carries announcement risk while the market is closed. Event-risk filtering must be reproducible and fail closed when its source data is unavailable. Providers sync every 300 seconds and become stale after 1,800 seconds by default.

## Decision: Email and Enterprise WeChat notifications

**Decision**: Provide SMTP email and Enterprise WeChat webhook channels with append-only delivery records and up to three delivery attempts.

**Rationale**: The user requested both channels. Notification failure must be visible but must not block order/risk processing. Secrets are referenced from environment or secret storage and are never written to logs.

## Decision: Simulation and LIVE account modeling

**Decision**: v1 uses one default simulation account and masked LIVE account mappings.

**Rationale**: A single default simulation account keeps v1 understandable while still supporting cash, exposure, P&L, fees, slippage, and account snapshots. LIVE accounts must come from the broker gateway because GuPiao does not create real securities accounts or store broker passwords.

**Consequences**:

- Simulation account creation is automatic on first SIMULATION use.
- Simulation cash movements are append-only ledger entries.
- LIVE account numbers are masked in storage and UI.
- Disabled or read-only LIVE accounts block order submission.

## Decision: Docker Compose for Linux deployment

**Decision**: Use Docker Compose for Linux deployment of core services.

**Rationale**: Docker Compose makes the FastAPI backend, Vue/Nginx frontend, MySQL, and worker processes reproducible. It also keeps the Linux core independent from QMT runtime assumptions.

## Decision: Built-in and plugin strategies

**Decision**: Support both UI-configured built-in strategies and custom Python plugin strategies.

**Rationale**: The user explicitly wants strategy selection and custom strategy support. Built-ins provide a safe path for "一夜持股法"; plugins provide extensibility.

**Consequences**:

- Plugins need metadata and parameter schema validation before activation.
- Plugin signals must use the same normalized signal format and risk path as built-ins.
- Invalid plugins must be visible but disabled.
- Only the administrator can register or execute plugins.
- v1 loads plugins from a trusted local directory and does not accept arbitrary browser uploads.
