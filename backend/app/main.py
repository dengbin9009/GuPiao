from __future__ import annotations

import ipaddress
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Cookie, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from .config import Settings, get_settings, live_runtime_is_open
from .database import Base, SessionLocal, apply_runtime_migrations, engine, get_db
from .data_sync import sync_stock_master
from .brokers import build_broker_adapter
from .live_trading import sync_live_accounts as sync_broker_accounts
from .market_data import MarketDataError
from .market_cache import quote_is_stale, read_bar_cache, refresh_bar_cache
from .models import (
    AccountSnapshot,
    Administrator,
    BacktestRun,
    BacktestTrade,
    BrokerGateway,
    DataSourceState,
    Fill,
    GatewayEvent,
    LiveTradingAccount,
    NotificationChannel,
    NotificationDelivery,
    Order,
    Position,
    ProbabilityCandidateDecision,
    ProbabilityPortfolioRun,
    RiskEvent,
    RiskSettings,
    SimulationAccount,
    SimulationAccountLedger,
    Stock,
    StockEvent,
    StrategyConfig,
    StrategyDefinition,
    StrategyLog,
    StrategyRun,
    StrategySchedule,
    TradingAgentBatch,
    TradingAgentCandidateAnalysis,
    TradingAgentPortfolioDecision,
    WatchlistItem,
    now,
)
from .notifications import deliver_channel
from .worker import poll_corporate_events, poll_watchlist_quotes
from .providers import market_router, trading_calendar_service
from .security import create_session, read_session, verify_password
from .services import (
    create_backtest,
    execute_simulation_strategy,
    generated_bars,
    seed_database,
    serialize,
    snapshot_account,
    stock_payload,
    sync_plugin_definitions,
    validate_strategy_parameters,
    watchlist_payload,
)
from .trading_agents.runtime import find_matching_dry_run, simulation_account_is_available
from .trading_agents.batches import create_batch
from .trading_agents.config import (
    ANALYSIS_PROFILES,
    POSITION_MAPPINGS,
    readiness as trading_agents_readiness,
    validate_parameters as validate_trading_agents_parameters,
)
from .trading_agents.rebalance import rebalance_batch
from .strategy_execution import execute_strategy_trigger
from .probability_portfolio.candidates import build_scored_candidates
from .probability_portfolio.config import PROBABILITY_PORTFOLIO_DEFAULTS
from .probability_portfolio.execution import execute_portfolio_entry
from .probability_portfolio.readiness import (
    automation_readiness as probability_automation_readiness,
)
from .probability_portfolio.runtime import (
    STRATEGY_KEY as PROBABILITY_PORTFOLIO_KEY,
    seed_probability_portfolio_runtime,
)
from .runtime_bootstrap import seed_strategy_runtimes
settings = get_settings()


def require_live_runtime_open() -> None:
    if not live_runtime_is_open(settings):
        raise HTTPException(
            status_code=403,
            detail="运行配置未启用真实盘，LIVE 写操作已禁止",
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    apply_runtime_migrations()
    with SessionLocal() as db:
        seed_database(db, settings)
        seed_strategy_runtimes(db, settings)
    yield


app = FastAPI(title="GuPiao API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def ip_allowlist(request: Request, call_next):
    if settings.allowed_ips and request.url.path.startswith("/api/"):
        host = request.client.host if request.client else "127.0.0.1"
        try:
            address = ipaddress.ip_address(host)
            networks = [ipaddress.ip_network(item.strip()) for item in settings.allowed_ips.split(",") if item.strip()]
            if not any(address in network for network in networks):
                raise HTTPException(status_code=403, detail="当前 IP 不在访问白名单")
        except ValueError:
            pass
    return await call_next(request)


def require_admin(
    gupiao_session: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
    config: Settings = Depends(get_settings),
) -> Administrator:
    payload = read_session(gupiao_session, config.secret_key)
    if not payload:
        raise HTTPException(status_code=401, detail="请先登录")
    admin = db.scalar(
        select(Administrator).where(
            Administrator.username == payload["sub"], Administrator.is_active.is_(True)
        )
    )
    if not admin:
        raise HTTPException(status_code=401, detail="登录状态无效")
    return admin


class LoginRequest(BaseModel):
    username: str
    password: str


class WatchlistCreate(BaseModel):
    symbol: str
    note: str | None = None


class StrategyConfigCreate(BaseModel):
    strategy_key: str
    name: str
    mode: str = "SIMULATION"
    parameters: dict[str, Any] = Field(default_factory=dict)


class ScheduleCreate(BaseModel):
    strategy_config_id: int
    trigger_type: str
    run_time: str
    enabled: bool = False


class ScheduleUpdate(BaseModel):
    trigger_type: str | None = None
    run_time: str | None = None
    enabled: bool | None = None


class SimulationReset(BaseModel):
    initial_cash: float = Field(gt=0)
    reason: str = "管理员重置模拟账户"


class BacktestCreate(BaseModel):
    strategy_key: str = "overnight_hold"
    parameters: dict[str, Any] = Field(default_factory=dict)
    universe: dict[str, Any] = Field(default_factory=dict)
    benchmark_symbol: str = "000300.SH"
    timeframe: str = "1m"
    start_date: str = "2025-01-01"
    end_date: str = "2025-12-31"
    adjustment_mode: str = "qfq"
    initial_cash: float = 10000


class NotificationCreate(BaseModel):
    type: str
    name: str
    recipient: str
    secret_ref: str
    event_types: list[str] = Field(default_factory=list)


class LiveAccountUpdate(BaseModel):
    enabled: bool | None = None
    read_only: bool | None = None


class LiveModeUpdate(BaseModel):
    enabled: bool
    confirmation: str = ""


class TradingAgentsBatchCreate(BaseModel):
    strategy_config_id: int | None = None


class TradingAgentsConfigUpdate(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=dict)
    simulation_account_id: int | None = None


class SimulationAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    initial_cash: float = Field(default=100_000, gt=0)


class ProbabilityPortfolioConfigUpdate(BaseModel):
    mode: str = "SIMULATION"
    parameters: dict[str, Any] = Field(default_factory=dict)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "gupiao"}


def _probability_portfolio_config(db: Session) -> StrategyConfig:
    config = db.scalar(
        select(StrategyConfig)
        .join(StrategyDefinition)
        .where(StrategyDefinition.key == PROBABILITY_PORTFOLIO_KEY)
        .limit(1)
    )
    if config is None:
        config = seed_probability_portfolio_runtime(db, settings)
    return config


def _probability_readiness(db: Session) -> dict[str, Any]:
    config = _probability_portfolio_config(db)
    check = probability_automation_readiness(
        db,
        config,
        settings,
        current=now(),
    )
    account = check["account"]
    artifact = check["artifact"]
    dry_run = check["dry_run"]
    schedules = {
        item.trigger_type: item
        for item in db.scalars(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id
            )
        )
    }
    return {
        "ready": check["automation_ready"],
        "reasons": check["automation_reasons"],
        "automation_ready": check["automation_ready"],
        "automation_reasons": check["automation_reasons"],
        "simulation_only": check["simulation_only"],
        "account_id": account.id if account else None,
        "initial_cash": account.initial_cash if account else None,
        "model_ready": artifact is not None,
        "model_version": artifact.model_version if artifact else None,
        "brier_score": artifact.brier_score if artifact else None,
        "training_sample_count": artifact.training_sample_count if artifact else 0,
        "calibration_sample_count": (
            artifact.calibration_sample_count if artifact else 0
        ),
        "dry_run_validated": dry_run is not None,
        "last_dry_run_id": dry_run.id if dry_run else None,
        "entry_schedule_enabled": bool(
            schedules.get("portfolio_entry") and schedules["portfolio_entry"].enabled
        ),
        "exit_schedule_enabled": bool(
            schedules.get("portfolio_exit") and schedules["portfolio_exit"].enabled
        ),
        "parameters": {**PROBABILITY_PORTFOLIO_DEFAULTS, **(config.parameters or {})},
    }


@app.get("/api/probability-portfolio/readiness")
def get_probability_portfolio_readiness(
    _: Administrator = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return _probability_readiness(db)


@app.put("/api/probability-portfolio/config")
def update_probability_portfolio_config(
    payload: ProbabilityPortfolioConfigUpdate,
    _: Administrator = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if payload.mode != "SIMULATION":
        raise HTTPException(status_code=422, detail="概率组合策略仅支持模拟盘")
    config = _probability_portfolio_config(db)
    allowed = set(PROBABILITY_PORTFOLIO_DEFAULTS) | {"prefilter_size", "quote_max_age_seconds"}
    unknown = sorted(set(payload.parameters) - allowed)
    if unknown:
        raise HTTPException(status_code=422, detail=f"未知概率组合参数: {', '.join(unknown)}")
    parameters = {**PROBABILITY_PORTFOLIO_DEFAULTS, **(config.parameters or {}), **payload.parameters}
    if not 1 <= int(parameters["max_positions"]) <= 10:
        raise HTTPException(status_code=422, detail="最大持股数量必须在1至10之间")
    if not 0.55 <= float(parameters["min_probability"]) <= 1:
        raise HTTPException(status_code=422, detail="最低校准盈利概率不得低于55%")
    if float(parameters["min_expected_net_return"]) < 0:
        raise HTTPException(status_code=422, detail="最低预期净收益不得低于0")
    if not 0.02 <= float(parameters["min_position_pct"]) <= float(
        parameters["max_position_pct"]
    ) <= 0.36:
        raise HTTPException(status_code=422, detail="单股仓位必须在2%至36%之间")
    if not 0.30 <= float(parameters["min_total_exposure_pct"]) <= float(
        parameters["max_total_exposure_pct"]
    ) <= 0.60:
        raise HTTPException(status_code=422, detail="组合总仓位必须在30%至60%之间")
    if not 1 <= int(parameters.get("prefilter_size", 100)) <= 100:
        raise HTTPException(status_code=422, detail="预筛数量必须在1至100之间")
    if str(parameters["entry_time"]) != "14:40" or str(
        parameters["latest_entry_time"]
    ) != "14:41":
        raise HTTPException(status_code=422, detail="入场时间固定为14:40至14:41")
    if str(parameters["exit_time"]) != "10:30" or str(
        parameters["latest_exit_time"]
    ) != "10:45":
        raise HTTPException(status_code=422, detail="退出时间固定为10:30至10:45")
    if int(parameters["retry_seconds"]) != 15:
        raise HTTPException(status_code=422, detail="退出重试间隔固定为15秒")
    if int(parameters["min_training_samples"]) < 500:
        raise HTTPException(status_code=422, detail="训练样本不得少于500条")
    if int(parameters["min_calibration_samples"]) < 100:
        raise HTTPException(status_code=422, detail="校准样本不得少于100条")
    if not 0 < float(parameters["max_brier_score"]) <= 0.25:
        raise HTTPException(status_code=422, detail="最大Brier分数不得超过0.25")
    if str(parameters["feature_version"]) != str(
        PROBABILITY_PORTFOLIO_DEFAULTS["feature_version"]
    ):
        raise HTTPException(status_code=422, detail="特征版本不可由配置接口修改")
    if not 0 < float(parameters["daily_loss_limit_pct"]) <= 0.10:
        raise HTTPException(status_code=422, detail="日亏损熔断必须在0至10%之间")
    if not 1 <= int(parameters.get("quote_max_age_seconds", 60)) <= 60:
        raise HTTPException(status_code=422, detail="行情新鲜度必须在1至60秒之间")
    if not 1 <= int(parameters["event_max_age_seconds"]) <= 1800:
        raise HTTPException(status_code=422, detail="公告新鲜度必须在1至1800秒之间")
    config.mode = "SIMULATION"
    config.parameters = parameters
    for schedule in db.scalars(
        select(StrategySchedule).where(
            StrategySchedule.strategy_config_id == config.id,
            StrategySchedule.trigger_type == "portfolio_entry",
        )
    ):
        schedule.enabled = False
        schedule.last_scheduled_for = None
        schedule.next_run_at = None
    db.commit()
    return serialize(config)


@app.post("/api/probability-portfolio/dry-run", status_code=202)
def run_probability_portfolio_dry_run(
    _: Administrator = Depends(require_admin),
    db: Session = Depends(get_db),
):
    config = _probability_portfolio_config(db)
    current = now()
    candidates = build_scored_candidates(db, config, current=current)
    run = execute_portfolio_entry(
        db,
        config,
        current=current,
        scored_candidates=candidates.scored,
        rejected_candidates=candidates.rejected,
        candidate_reasons=candidates.reasons,
        trigger_type=f"portfolio_dry_{current:%H%M%S%f}",
        dry_run=True,
    )
    return serialize(run)


@app.get("/api/probability-portfolio/runs")
def list_probability_portfolio_runs(
    limit: int = Query(default=30, ge=1, le=100),
    _: Administrator = Depends(require_admin),
    db: Session = Depends(get_db),
):
    runs = list(
        db.scalars(
            select(ProbabilityPortfolioRun)
            .order_by(ProbabilityPortfolioRun.id.desc())
            .limit(limit)
        )
    )
    return [serialize(item) for item in runs]


@app.get("/api/probability-portfolio/runs/{run_id}")
def get_probability_portfolio_run(
    run_id: int,
    _: Administrator = Depends(require_admin),
    db: Session = Depends(get_db),
):
    portfolio_run = db.get(ProbabilityPortfolioRun, run_id)
    if portfolio_run is None:
        raise HTTPException(status_code=404, detail="概率组合运行不存在")
    strategy_run = db.get(StrategyRun, portfolio_run.strategy_run_id)
    decisions = []
    for item in db.scalars(
        select(ProbabilityCandidateDecision)
        .where(ProbabilityCandidateDecision.portfolio_run_id == portfolio_run.id)
        .order_by(
            ProbabilityCandidateDecision.rank.is_(None),
            ProbabilityCandidateDecision.rank,
            ProbabilityCandidateDecision.id,
        )
    ):
        stock = db.get(Stock, item.stock_id)
        decisions.append(
            serialize(
                item,
                symbol=stock.symbol if stock else str(item.stock_id),
                name=stock.name if stock else "未知股票",
            )
        )
    order_ids = list(portfolio_run.order_ids or [])
    orders = [
        serialize(
            order,
            symbol=(db.get(Stock, order.stock_id).symbol),
            name=(db.get(Stock, order.stock_id).name),
        )
        for order in db.scalars(
            select(Order).where(Order.id.in_(order_ids)).order_by(Order.id)
        )
    ] if order_ids else []
    return serialize(
        portfolio_run,
        strategy_run=serialize(strategy_run) if strategy_run else None,
        decisions=decisions,
        orders=orders,
    )


@app.post("/api/auth/login", status_code=204)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)) -> None:
    admin = db.scalar(select(Administrator).where(Administrator.username == payload.username))
    if not admin or not admin.is_active or not verify_password(payload.password, admin.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    admin.last_login_at = now()
    db.commit()
    response.set_cookie(
        "gupiao_session",
        create_session(admin.username, settings.secret_key),
        httponly=True,
        samesite="lax",
        secure=settings.environment == "production",
        max_age=8 * 60 * 60,
    )


@app.post("/api/auth/logout", status_code=204)
def logout(response: Response) -> None:
    response.delete_cookie("gupiao_session")


@app.get("/api/dashboard")
def dashboard(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    account = db.scalar(select(SimulationAccount).limit(1))
    risk = db.scalar(select(RiskSettings).where(RiskSettings.mode == "LIVE"))
    gateways = list(db.scalars(select(BrokerGateway).order_by(BrokerGateway.id)))
    return {
        "mode": "LIVE" if risk and risk.live_enabled else "SIMULATION",
        "account": serialize(account) if account else None,
        "watchlist_count": len(list(db.scalars(select(WatchlistItem.id)))),
        "strategy_count": len(list(db.scalars(select(StrategyDefinition.id)))),
        "open_order_count": len(
            list(db.scalars(select(Order.id).where(Order.status.in_(["created", "submitted", "partially_filled"]))))
        ),
        "risk": serialize(risk) if risk else None,
        "gateways": [serialize(item) for item in gateways],
    }


@app.get("/api/stocks/search")
def search_stocks(q: str = Query(min_length=1), _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    term = f"%{q.strip().lower()}%"
    stocks = db.scalars(
        select(Stock)
        .where(
            or_(
                Stock.code.like(term),
                Stock.symbol.ilike(term),
                Stock.name.like(term),
                Stock.pinyin.ilike(term),
                Stock.pinyin_initials.ilike(term),
            )
        )
        .limit(20)
    )
    return [stock_payload(stock) for stock in stocks]


@app.get("/api/stocks/{symbol}/events")
def stock_events(symbol: str, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    stock = db.scalar(select(Stock).where(Stock.symbol == symbol))
    if not stock:
        raise HTTPException(status_code=404, detail="股票不存在")
    events = db.scalars(select(StockEvent).where(StockEvent.stock_id == stock.id).order_by(StockEvent.published_at.desc()))
    return [serialize(item, symbol=stock.symbol) for item in events]


@app.get("/api/watchlist")
def list_watchlist(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    return [watchlist_payload(db, item) for item in db.scalars(select(WatchlistItem).order_by(WatchlistItem.id))]


@app.post("/api/watchlist/refresh")
def refresh_watchlist(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    result = poll_watchlist_quotes()
    if result["errors"]:
        raise HTTPException(status_code=503, detail="实时行情提供方全部不可用")
    return result


@app.post("/api/market-data/stocks/sync")
def sync_stocks(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        provider = market_router().select("stock_master")
        result = sync_stock_master(db, provider)
    except MarketDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"provider": provider.name, "created": result.created, "updated": result.updated}


@app.post("/api/market-data/events/sync")
def sync_events(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    result = poll_corporate_events()
    if result["errors"]:
        raise HTTPException(status_code=503, detail="公司公告提供方全部不可用")
    return {"provider": "akshare_events", **result}


@app.post("/api/watchlist", status_code=201)
def add_watchlist(payload: WatchlistCreate, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    stock = db.scalar(select(Stock).where(or_(Stock.symbol == payload.symbol, Stock.code == payload.symbol)))
    if not stock:
        raise HTTPException(status_code=404, detail="股票不存在")
    if db.scalar(select(WatchlistItem).where(WatchlistItem.stock_id == stock.id)):
        raise HTTPException(status_code=409, detail="股票已在特别关注中")
    item = WatchlistItem(stock_id=stock.id, note=payload.note)
    db.add(item)
    db.commit()
    db.refresh(item)
    return watchlist_payload(db, item)


@app.delete("/api/watchlist/{item_id}", status_code=204)
def remove_watchlist(item_id: int, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    item = db.get(WatchlistItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="关注项不存在")
    db.delete(item)
    db.commit()


@app.get("/api/strategies")
def list_strategies(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    return [serialize(item) for item in db.scalars(select(StrategyDefinition).order_by(StrategyDefinition.id))]


def _trading_agents_config(db: Session, config_id: int | None = None) -> StrategyConfig:
    query = (
        select(StrategyConfig)
        .join(StrategyDefinition)
        .where(StrategyDefinition.key == "trading_agents_auto")
    )
    if config_id is not None:
        query = query.where(StrategyConfig.id == config_id)
    config = db.scalar(query.order_by(StrategyConfig.id).limit(1))
    if not config:
        raise HTTPException(status_code=404, detail="TradingAgents 策略配置不存在")
    return config


def _trading_agents_runtime_readiness(
    db: Session,
    config: StrategyConfig,
) -> dict[str, Any]:
    result = trading_agents_readiness(settings)
    latest_dry_run = find_matching_dry_run(db, config)
    automation_reasons = list(result["reasons"])
    if config.mode != "SIMULATION" or not simulation_account_is_available(
        db,
        account_id=config.simulation_account_id,
        strategy_config_id=config.id,
    ):
        automation_reasons.append("simulation_account_binding")
    if not config.enabled:
        automation_reasons.append("strategy_config_disabled")
    if latest_dry_run is None:
        automation_reasons.append("successful_dry_run")
    if bool((config.parameters or {}).get("dry_run", True)):
        automation_reasons.append("dry_run_mode_enabled")
    return {
        **result,
        "strategy_config_id": config.id,
        "simulation_account_id": config.simulation_account_id,
        "dry_run_validated": latest_dry_run is not None,
        "last_dry_run_batch_id": latest_dry_run.id if latest_dry_run else None,
        "automation_ready": not automation_reasons,
        "automation_reasons": automation_reasons,
    }


def _batch_payload(db: Session, batch: TradingAgentBatch, *, detail: bool = False):
    result = serialize(batch)
    result["analysis_status_counts"] = {
        status: count
        for status, count in db.execute(
            select(
                TradingAgentCandidateAnalysis.status,
                func.count(TradingAgentCandidateAnalysis.id),
            )
            .where(TradingAgentCandidateAnalysis.batch_id == batch.id)
            .group_by(TradingAgentCandidateAnalysis.status)
        )
    }
    if not detail:
        return result
    analyses = list(
        db.scalars(
            select(TradingAgentCandidateAnalysis)
            .where(TradingAgentCandidateAnalysis.batch_id == batch.id)
            .order_by(
                TradingAgentCandidateAnalysis.rank.is_(None),
                TradingAgentCandidateAnalysis.rank,
                TradingAgentCandidateAnalysis.id,
            )
        )
    )
    stocks = {
        stock.id: stock
        for stock in db.scalars(
            select(Stock).where(Stock.id.in_({item.stock_id for item in analyses}))
        )
    } if analyses else {}
    decision = db.scalar(
        select(TradingAgentPortfolioDecision).where(
            TradingAgentPortfolioDecision.batch_id == batch.id
        )
    )
    result["analyses"] = [
        serialize(item, symbol=stocks[item.stock_id].symbol, name=stocks[item.stock_id].name)
        for item in analyses
    ]
    result["portfolio_decision"] = serialize(decision) if decision else None
    orders = list(
        db.scalars(
            select(Order)
            .where(Order.id.in_(batch.order_ids or []))
            .order_by(Order.id)
        )
    )
    order_stocks = {
        stock.id: stock
        for stock in db.scalars(
            select(Stock).where(Stock.id.in_({item.stock_id for item in orders}))
        )
    } if orders else {}
    result["orders"] = [
        serialize(
            item,
            symbol=order_stocks[item.stock_id].symbol,
            name=order_stocks[item.stock_id].name,
        )
        for item in orders
    ]
    return result


@app.get("/api/trading-agents/readiness")
def get_trading_agents_readiness(
    _: Administrator = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return _trading_agents_runtime_readiness(db, _trading_agents_config(db))


@app.get("/api/trading-agents/profiles")
def get_trading_agents_profiles(_: Administrator = Depends(require_admin)):
    return {
        "analysis_profiles": ANALYSIS_PROFILES,
        "position_mappings": POSITION_MAPPINGS,
    }


@app.put("/api/trading-agents/config")
def update_trading_agents_config(
    payload: TradingAgentsConfigUpdate,
    _: Administrator = Depends(require_admin),
    db: Session = Depends(get_db),
):
    config = _trading_agents_config(db)
    try:
        config.parameters = validate_trading_agents_parameters(payload.parameters)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload.simulation_account_id is not None:
        account = db.get(SimulationAccount, payload.simulation_account_id)
        if not account or account.status != "active":
            raise HTTPException(status_code=422, detail="模拟账户不存在或不可用")
        if not simulation_account_is_available(
            db,
            account_id=account.id,
            strategy_config_id=config.id,
        ):
            raise HTTPException(
                status_code=422,
                detail="TradingAgents 必须绑定未被其他策略配置占用的独立账户",
            )
        config.simulation_account_id = account.id
    config.mode = "SIMULATION"
    for schedule in db.scalars(
        select(StrategySchedule).where(
            StrategySchedule.strategy_config_id == config.id,
            StrategySchedule.trigger_type.in_(["agent_analysis", "agent_rebalance"]),
        )
    ):
        schedule.enabled = False
        schedule.last_scheduled_for = None
        schedule.next_run_at = None
    db.commit()
    return serialize(config)


@app.post("/api/trading-agents/batches", status_code=202)
def create_trading_agents_batch(
    payload: TradingAgentsBatchCreate,
    _: Administrator = Depends(require_admin),
    db: Session = Depends(get_db),
):
    check = trading_agents_readiness(settings)
    if not check["ready"]:
        raise HTTPException(
            status_code=503,
            detail=f"TradingAgents 尚未就绪: {', '.join(check['reasons'])}",
        )
    config = _trading_agents_config(db, payload.strategy_config_id)
    try:
        batch = create_batch(db, config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _batch_payload(db, batch)


@app.get("/api/trading-agents/batches")
def list_trading_agents_batches(
    limit: int = Query(default=30, ge=1, le=100),
    _: Administrator = Depends(require_admin),
    db: Session = Depends(get_db),
):
    batches = db.scalars(
        select(TradingAgentBatch).order_by(TradingAgentBatch.id.desc()).limit(limit)
    )
    return [_batch_payload(db, item) for item in batches]


@app.get("/api/trading-agents/batches/{batch_id}")
def get_trading_agents_batch(
    batch_id: int,
    _: Administrator = Depends(require_admin),
    db: Session = Depends(get_db),
):
    batch = db.get(TradingAgentBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="TradingAgents 批次不存在")
    return _batch_payload(db, batch, detail=True)


@app.post("/api/trading-agents/batches/{batch_id}/cancel")
def cancel_trading_agents_batch(
    batch_id: int,
    _: Administrator = Depends(require_admin),
    db: Session = Depends(get_db),
):
    batch = db.get(TradingAgentBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="TradingAgents 批次不存在")
    if batch.status not in {"pending", "processing", "ready"}:
        raise HTTPException(status_code=409, detail="当前批次状态不能取消")
    batch.status = "cancelled"
    batch.error_message = "管理员取消批次"
    batch.lease_until = None
    batch.completed_at = now()
    db.commit()
    return _batch_payload(db, batch)


@app.post("/api/trading-agents/batches/{batch_id}/dry-run")
def dry_run_trading_agents_batch(
    batch_id: int,
    _: Administrator = Depends(require_admin),
    db: Session = Depends(get_db),
):
    batch = db.get(TradingAgentBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="TradingAgents 批次不存在")
    if batch.status != "ready":
        raise HTTPException(status_code=409, detail="批次尚未完成全部分析")
    config = db.get(StrategyConfig, batch.strategy_config_id)
    if not bool((config.parameters or {}).get("dry_run", True)):
        raise HTTPException(status_code=409, detail="策略配置未启用无下单演练")
    run = rebalance_batch(db, batch, allow_outside_window=True)
    return serialize(run)


@app.post("/api/plugins/scan")
def plugins_scan(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    return sync_plugin_definitions(db, settings.trusted_plugin_dir)


@app.get("/api/strategy-configs")
def strategy_configs(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    return [
        serialize(
            item,
            strategy_key=db.get(StrategyDefinition, item.strategy_definition_id).key,
        )
        for item in db.scalars(select(StrategyConfig).order_by(StrategyConfig.id.desc()))
    ]


@app.post("/api/strategy-configs", status_code=201)
def create_strategy_config(payload: StrategyConfigCreate, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == payload.strategy_key, StrategyDefinition.enabled.is_(True)))
    if not definition:
        raise HTTPException(status_code=422, detail="策略不存在或不可用")
    if definition.key == "trading_agents_auto":
        raise HTTPException(
            status_code=422,
            detail="TradingAgents 仅支持模拟盘的系统独立配置，请使用专用配置接口",
        )
    if definition.key == PROBABILITY_PORTFOLIO_KEY:
        raise HTTPException(
            status_code=422,
            detail="概率组合仅支持模拟盘的系统独立配置，请使用专用配置接口",
        )
    if payload.mode not in {"SIMULATION", "LIVE"}:
        raise HTTPException(status_code=422, detail="交易模式无效")
    if payload.mode == "LIVE":
        require_live_runtime_open()
    try:
        parameters = validate_strategy_parameters(definition.parameter_schema, payload.parameters)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    config = StrategyConfig(
        strategy_definition_id=definition.id,
        name=payload.name,
        mode=payload.mode,
        parameters=parameters,
        simulation_account_id=(
            db.scalar(select(SimulationAccount.id).order_by(SimulationAccount.id).limit(1))
            if payload.mode == "SIMULATION"
            else None
        ),
    )
    db.add(config)
    db.flush()
    if definition.key == "overnight_hold":
        db.add_all(
            [
                StrategySchedule(
                    strategy_config_id=config.id,
                    trigger_type="entry_evaluation",
                    run_time=str(config.parameters.get("evaluation_time", "14:40")),
                    enabled=False,
                ),
                StrategySchedule(
                    strategy_config_id=config.id,
                    trigger_type="exit_evaluation",
                    run_time=str(config.parameters.get("exit_evaluation_time", "09:35")),
                    enabled=False,
                ),
            ]
        )
    db.commit()
    db.refresh(config)
    return serialize(config)


@app.post("/api/strategy-configs/{config_id}/run", status_code=202)
def run_strategy(config_id: int, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    config = db.get(StrategyConfig, config_id)
    if not config or not config.enabled:
        raise HTTPException(status_code=422, detail="策略配置不可运行")
    if config.mode == "LIVE":
        require_live_runtime_open()
    definition = db.get(StrategyDefinition, config.strategy_definition_id)
    if definition and definition.key == "trading_agents_auto":
        check = trading_agents_readiness(settings)
        if not check["ready"]:
            raise HTTPException(
                status_code=503,
                detail=f"TradingAgents 尚未就绪: {', '.join(check['reasons'])}",
            )
        run = execute_strategy_trigger(db, config, "agent_analysis")
    elif definition and definition.key == PROBABILITY_PORTFOLIO_KEY:
        raise HTTPException(
            status_code=422,
            detail="概率组合请使用专用无下单演练或自动计划",
        )
    else:
        run = execute_simulation_strategy(db, config)
    if run.status == "failed" and config.mode == "LIVE":
        raise HTTPException(status_code=403, detail=run.error_message)
    return serialize(run)


@app.get("/api/strategy-runs")
def strategy_runs(mode: str | None = None, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    query = select(StrategyRun).order_by(StrategyRun.id.desc())
    if mode:
        query = query.where(StrategyRun.mode == mode)
    return [serialize(item) for item in db.scalars(query)]


@app.get("/api/strategy-runs/{run_id}/logs")
def strategy_logs(run_id: int, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    return [serialize(item) for item in db.scalars(select(StrategyLog).where(StrategyLog.strategy_run_id == run_id).order_by(StrategyLog.id))]


@app.get("/api/strategy-schedules")
def schedules(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    result = []
    for item in db.scalars(select(StrategySchedule).order_by(StrategySchedule.id)):
        config = db.get(StrategyConfig, item.strategy_config_id)
        definition = db.get(StrategyDefinition, config.strategy_definition_id)
        result.append(serialize(item, strategy_key=definition.key))
    return result


@app.post("/api/strategy-schedules", status_code=201)
def create_schedule(payload: ScheduleCreate, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    if payload.trigger_type not in {"entry_evaluation", "exit_evaluation", "custom"}:
        raise HTTPException(status_code=422, detail="调度触发类型无效")
    config = db.get(StrategyConfig, payload.strategy_config_id)
    if not config:
        raise HTTPException(status_code=404, detail="策略配置不存在")
    if config.mode == "LIVE" and payload.enabled:
        require_live_runtime_open()
    item = StrategySchedule(
        strategy_config_id=payload.strategy_config_id,
        trigger_type=payload.trigger_type,
        run_time=payload.run_time,
        enabled=payload.enabled,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return serialize(item)


@app.put("/api/strategy-schedules/{schedule_id}")
def update_schedule(schedule_id: int, payload: ScheduleUpdate, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    item = db.get(StrategySchedule, schedule_id)
    if not item:
        raise HTTPException(status_code=404, detail="调度不存在")
    config = db.get(StrategyConfig, item.strategy_config_id)
    definition = db.get(StrategyDefinition, config.strategy_definition_id) if config else None
    if definition and definition.key == "trading_agents_auto":
        fixed_times = {"agent_analysis": "13:30", "agent_rebalance": "14:45"}
        requested_trigger = payload.trigger_type or item.trigger_type
        requested_time = payload.run_time or item.run_time
        if (
            requested_trigger != item.trigger_type
            or requested_time != fixed_times.get(item.trigger_type)
        ):
            raise HTTPException(
                status_code=422,
                detail="TradingAgents 调度触发类型和时间固定，只允许启停",
            )
    if definition and definition.key == PROBABILITY_PORTFOLIO_KEY:
        fixed_times = {"portfolio_entry": "14:40", "portfolio_exit": "10:30"}
        requested_trigger = payload.trigger_type or item.trigger_type
        requested_time = payload.run_time or item.run_time
        if (
            requested_trigger != item.trigger_type
            or requested_time != fixed_times.get(item.trigger_type)
        ):
            raise HTTPException(
                status_code=422,
                detail="概率组合调度触发类型和时间固定，只允许启停",
            )
    if config and config.mode == "LIVE" and payload.enabled:
        require_live_runtime_open()
    if config and payload.enabled:
        if definition and definition.key == "trading_agents_auto":
            check = _trading_agents_runtime_readiness(db, config)
            if not check["automation_ready"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"自动计划不能启用: {', '.join(check['automation_reasons'])}",
                )
        if (
            definition
            and definition.key == PROBABILITY_PORTFOLIO_KEY
            and item.trigger_type == "portfolio_entry"
        ):
            exit_schedule = db.scalar(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id == config.id,
                    StrategySchedule.trigger_type == "portfolio_exit",
                    StrategySchedule.enabled.is_(True),
                )
            )
            if exit_schedule is None:
                raise HTTPException(
                    status_code=409,
                    detail="自动计划不能启用: 请先启用10:30退出计划",
                )
            check = probability_automation_readiness(
                db,
                config,
                settings,
                current=now(),
            )
            if not check["automation_ready"]:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "自动计划不能启用: "
                        f"{', '.join(check['automation_reasons'])}"
                    ),
                )
    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(item, key, value)
    if (
        definition
        and definition.key == PROBABILITY_PORTFOLIO_KEY
        and item.trigger_type == "portfolio_exit"
        and payload.enabled is False
    ):
        entry_schedule = db.scalar(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id,
                StrategySchedule.trigger_type == "portfolio_entry",
            )
        )
        if entry_schedule is not None:
            entry_schedule.enabled = False
            entry_schedule.last_scheduled_for = None
            entry_schedule.next_run_at = None
    db.commit()
    db.refresh(item)
    return serialize(item)


@app.delete("/api/strategy-schedules/{schedule_id}", status_code=204)
def delete_schedule(schedule_id: int, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    item = db.get(StrategySchedule, schedule_id)
    if not item:
        raise HTTPException(status_code=404, detail="调度不存在")
    db.delete(item)
    db.commit()


@app.get("/api/backtests")
def backtests(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    return [serialize(item) for item in db.scalars(select(BacktestRun).order_by(BacktestRun.id.desc()))]


@app.post("/api/backtests", status_code=202)
def start_backtest(payload: BacktestCreate, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == payload.strategy_key))
    if not definition:
        raise HTTPException(status_code=404, detail="策略不存在")
    try:
        run = create_backtest(db, definition, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return serialize(run)


@app.get("/api/backtests/{backtest_id}")
def backtest_detail(backtest_id: int, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    item = db.get(BacktestRun, backtest_id)
    if not item:
        raise HTTPException(status_code=404, detail="回测不存在")
    trades = db.scalars(select(BacktestTrade).where(BacktestTrade.backtest_run_id == backtest_id).order_by(BacktestTrade.id))
    curve_rows: list[dict[str, Any]] = []
    if item.equity_curve_uri:
        curve_path = (Path(__file__).resolve().parents[1] / ".." / item.equity_curve_uri).resolve()
        if curve_path.exists():
            try:
                import pandas as pd

                curve_rows = list(pd.read_parquet(curve_path).to_dict(orient="records"))
            except Exception:
                curve_rows = []
    return serialize(
        item,
        trades=[serialize(row, symbol=db.get(Stock, row.stock_id).symbol) for row in trades],
        equity_curve=curve_rows,
    )


@app.get("/api/backtests/{backtest_id}/trades")
def backtest_trades(backtest_id: int, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(BacktestTrade).where(BacktestTrade.backtest_run_id == backtest_id).order_by(BacktestTrade.id))
    return [serialize(item, symbol=db.get(Stock, item.stock_id).symbol) for item in rows]


def _trading_payload(db: Session, item: Order | Fill | Position) -> dict[str, Any]:
    stock = db.get(Stock, item.stock_id)
    return serialize(item, symbol=stock.symbol if stock else "")


@app.get("/api/orders")
def orders(mode: str | None = None, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    query = select(Order).order_by(Order.id.desc())
    if mode:
        query = query.where(Order.mode == mode)
    return [_trading_payload(db, item) for item in db.scalars(query)]


@app.get("/api/fills")
def fills(mode: str | None = None, account_id: int | None = None, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    query = select(Fill).order_by(Fill.id.desc())
    if mode:
        query = query.where(Fill.mode == mode)
    if account_id:
        query = query.where(Fill.account_id == account_id)
    return [_trading_payload(db, item) for item in db.scalars(query)]


@app.get("/api/positions")
def positions(mode: str | None = None, account_id: int | None = None, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    query = select(Position).order_by(Position.id.desc())
    if mode:
        query = query.where(Position.mode == mode)
    if account_id:
        query = query.where(Position.account_id == account_id)
    return [_trading_payload(db, item) for item in db.scalars(query)]


@app.get("/api/simulation/account")
def simulation_account(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    account = db.scalar(select(SimulationAccount).limit(1))
    if not account:
        seed_database(db, settings)
        account = db.scalar(select(SimulationAccount).limit(1))
    return serialize(account)


def _account_payload(db: Session, account: SimulationAccount) -> dict[str, Any]:
    market_value = db.scalar(
        select(func.coalesce(func.sum(Position.market_value), 0)).where(
            Position.account_id == account.id,
            Position.mode == "SIMULATION",
            Position.quantity > 0,
        )
    ) or 0
    total_asset = float(account.cash_balance) + float(market_value)
    bound_configs = list(
        db.scalars(
            select(StrategyConfig).where(
                StrategyConfig.simulation_account_id == account.id,
            )
        )
    )
    bound_keys = [
        db.get(StrategyDefinition, config.strategy_definition_id).key
        for config in bound_configs
    ]
    return serialize(
        account,
        market_value=float(market_value),
        total_asset=total_asset,
        bound_strategy_keys=bound_keys,
        available_for_trading_agents=(
            not bound_keys or set(bound_keys) == {"trading_agents_auto"}
        ),
    )


@app.get("/api/simulation/accounts")
def simulation_accounts(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    return [
        _account_payload(db, item)
        for item in db.scalars(select(SimulationAccount).order_by(SimulationAccount.id))
    ]


@app.post("/api/simulation/accounts", status_code=201)
def create_simulation_account(
    payload: SimulationAccountCreate,
    _: Administrator = Depends(require_admin),
    db: Session = Depends(get_db),
):
    account_name = payload.name.strip()
    if not account_name:
        raise HTTPException(status_code=422, detail="模拟账户名称不能为空")
    if db.scalar(select(SimulationAccount.id).where(SimulationAccount.name == account_name)):
        raise HTTPException(status_code=409, detail="模拟账户名称已存在")
    account = SimulationAccount(
        name=account_name,
        initial_cash=payload.initial_cash,
        cash_balance=payload.initial_cash,
        available_cash=payload.initial_cash,
        total_asset=payload.initial_cash,
        commission_rate=settings.simulation_commission_rate,
        min_commission=settings.simulation_min_commission,
        stamp_tax_rate=settings.simulation_stamp_tax_rate,
        transfer_fee_rate=settings.simulation_transfer_fee_rate,
        slippage_bps=settings.simulation_slippage_bps,
    )
    db.add(account)
    db.flush()
    db.add(
        SimulationAccountLedger(
            simulation_account_id=account.id,
            event_type="initialize",
            amount=account.initial_cash,
            balance_after=account.initial_cash,
            message="管理员创建模拟账户",
        )
    )
    snapshot_account(db, account)
    db.commit()
    return _account_payload(db, account)


@app.get("/api/simulation/accounts/{account_id}")
def simulation_account_detail(
    account_id: int,
    _: Administrator = Depends(require_admin),
    db: Session = Depends(get_db),
):
    account = db.get(SimulationAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="模拟账户不存在")
    return _account_payload(db, account)


@app.post("/api/simulation/account/reset")
def reset_simulation(payload: SimulationReset, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    active = db.scalar(select(StrategyRun.id).where(StrategyRun.status.in_(["pending", "running"])).limit(1))
    open_order = db.scalar(select(Order.id).where(Order.status.in_(["created", "submitted", "partially_filled"])).limit(1))
    if active or open_order:
        raise HTTPException(status_code=409, detail="存在运行中策略或未完成订单，不能重置")
    account = db.scalar(select(SimulationAccount).limit(1))
    if not account:
        raise HTTPException(status_code=404, detail="模拟账户不存在")
    db.execute(delete(Position).where(Position.mode == "SIMULATION", Position.account_id == account.id))
    account.initial_cash = payload.initial_cash
    account.cash_balance = payload.initial_cash
    account.available_cash = payload.initial_cash
    account.frozen_cash = 0
    account.total_asset = payload.initial_cash
    account.realized_pnl = 0
    account.unrealized_pnl = 0
    db.add(
        SimulationAccountLedger(
            simulation_account_id=account.id,
            event_type="reset",
            amount=payload.initial_cash,
            balance_after=payload.initial_cash,
            message=payload.reason,
        )
    )
    snapshot_account(db, account)
    db.commit()
    db.refresh(account)
    return serialize(account)


@app.get("/api/live/accounts")
def live_accounts(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    return [serialize(item) for item in db.scalars(select(LiveTradingAccount).order_by(LiveTradingAccount.id))]


@app.post("/api/live/accounts/sync")
def sync_live_accounts(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    require_live_runtime_open()
    gateway = db.scalar(select(BrokerGateway).where(BrokerGateway.enabled.is_(True)).order_by(BrokerGateway.id))
    if not gateway:
        raise HTTPException(status_code=503, detail="没有已启用的真实盘适配器")
    adapter = build_broker_adapter(
        gateway.type,
        qmt_url=settings.qmt_url,
        qmt_token=settings.qmt_token,
        ptrade_url=settings.ptrade_url,
        ptrade_token=settings.ptrade_token,
        futu_host=settings.futu_host,
        futu_port=settings.futu_port,
        futu_trd_market=settings.futu_trd_market,
        futu_security_firm=settings.futu_security_firm,
        futu_trd_env=settings.futu_trd_env,
        futu_unlock_password=settings.futu_unlock_password,
    )
    result = sync_broker_accounts(db, gateway, adapter)
    if not result.gateway_healthy:
        raise HTTPException(status_code=503, detail=gateway.last_error or "真实盘网关不可用")
    return [serialize(item) for item in db.scalars(select(LiveTradingAccount).where(LiveTradingAccount.gateway_id == gateway.id))]


@app.put("/api/live/accounts/{account_id}")
def update_live_account(
    account_id: int,
    payload: LiveAccountUpdate,
    _: Administrator = Depends(require_admin),
    db: Session = Depends(get_db),
):
    account = db.get(LiveTradingAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="真实盘账户不存在")
    if payload.enabled is True or payload.read_only is False:
        require_live_runtime_open()
    if payload.enabled is not None:
        account.enabled = payload.enabled
    if payload.read_only is not None:
        account.read_only = payload.read_only
    db.commit()
    db.refresh(account)
    return serialize(account)


@app.put("/api/live/mode")
def update_live_mode(
    payload: LiveModeUpdate,
    _: Administrator = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if payload.enabled:
        require_live_runtime_open()
    risk = db.scalar(select(RiskSettings).where(RiskSettings.mode == "LIVE"))
    if not risk:
        raise HTTPException(status_code=404, detail="真实盘风控配置不存在")
    if payload.enabled:
        if payload.confirmation != "ENABLE LIVE":
            raise HTTPException(status_code=422, detail="真实盘启用确认文本无效")
        account = db.scalar(
            select(LiveTradingAccount).where(
                LiveTradingAccount.enabled.is_(True),
                LiveTradingAccount.read_only.is_(False),
            )
        )
        gateway = db.get(BrokerGateway, account.gateway_id) if account else None
        if not account or not gateway or not gateway.enabled or not gateway.healthy:
            raise HTTPException(status_code=409, detail="缺少可交易账户或健康网关")
        risk.emergency_stop_enabled = False
    risk.live_enabled = payload.enabled
    db.add(
        RiskEvent(
            mode="LIVE",
            event_type="mode_enabled" if payload.enabled else "mode_disabled",
            message="管理员启用真实盘" if payload.enabled else "管理员关闭真实盘",
            context={},
        )
    )
    db.commit()
    return serialize(risk)


@app.get("/api/accounts/snapshots")
def snapshots(mode: str | None = None, account_id: int | None = None, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    query = select(AccountSnapshot).order_by(AccountSnapshot.id.desc())
    if mode:
        query = query.where(AccountSnapshot.mode == mode)
    if account_id:
        query = query.where(AccountSnapshot.account_id == account_id)
    return [serialize(item) for item in db.scalars(query.limit(200))]


@app.get("/api/risk/settings")
def get_risk_settings(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    return [serialize(item) for item in db.scalars(select(RiskSettings).order_by(RiskSettings.mode))]


@app.put("/api/risk/settings")
def update_risk_settings(payload: dict[str, Any], _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    mode = payload.pop("mode", "LIVE")
    if mode == "LIVE" and payload.get("live_enabled") is True:
        require_live_runtime_open()
    item = db.scalar(select(RiskSettings).where(RiskSettings.mode == mode))
    if not item:
        raise HTTPException(status_code=404, detail="风控配置不存在")
    allowed = {column.key for column in item.__table__.columns} - {"id", "mode", "updated_at"}
    for key, value in payload.items():
        if key in allowed:
            setattr(item, key, value)
    db.commit()
    db.refresh(item)
    return serialize(item)


@app.get("/api/risk/events")
def risk_events(mode: str | None = None, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    query = select(RiskEvent).order_by(RiskEvent.id.desc())
    if mode:
        query = query.where(RiskEvent.mode == mode)
    return [serialize(item) for item in db.scalars(query.limit(200))]


@app.post("/api/risk/emergency-stop", status_code=204)
def emergency_stop(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    for item in db.scalars(select(RiskSettings)):
        item.emergency_stop_enabled = True
        if item.mode == "LIVE":
            item.live_enabled = False
    db.add(RiskEvent(mode="LIVE", event_type="emergency_stop", message="管理员触发紧急停止", context={}))
    db.commit()


@app.get("/api/gateways")
def gateways(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    capability_matrix = {
        "qmt": ["accounts", "orders", "positions", "a_share"],
        "ptrade": ["accounts", "orders", "positions", "a_share"],
        "futu_opend": ["accounts", "orders", "quotes"],
    }
    return [
        serialize(
            item,
            capabilities=capability_matrix.get(item.type, []),
            status="healthy" if item.healthy else "offline",
            adapter_name={
                "qmt": "QMT",
                "ptrade": "PTrade",
                "futu_opend": "Futu OpenD",
            }.get(item.type, item.type),
        )
        for item in db.scalars(select(BrokerGateway).order_by(BrokerGateway.id))
    ]


@app.get("/api/gateways/{gateway_id}/health")
def gateway_health(gateway_id: int, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    item = db.get(BrokerGateway, gateway_id)
    if not item:
        raise HTTPException(status_code=404, detail="网关不存在")
    return serialize(item)


@app.get("/api/gateway/events")
def gateway_events(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    return [serialize(item) for item in db.scalars(select(GatewayEvent).order_by(GatewayEvent.id.desc()).limit(200))]


@app.get("/api/market-data/sources")
def data_sources(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    return [serialize(item) for item in db.scalars(select(DataSourceState).order_by(DataSourceState.id))]


@app.get("/api/market-data/calendar")
def market_calendar(
    current_date: str | None = None,
    _: Administrator = Depends(require_admin),
):
    service = trading_calendar_service()
    target = datetime.fromisoformat(current_date).date() if current_date else now().date()
    return {
        "date": target.isoformat(),
        "is_trading_day": service.is_trading_day(target),
    }


@app.post("/api/market-data/stock-master/sync")
def sync_market_stock_master(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        provider = market_router().select("stock_master")
        result = sync_stock_master(db, provider)
    except MarketDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"created": result.created, "updated": result.updated, "provider": provider.name}


@app.get("/api/market-data/bars")
def market_bars(symbol: str, timeframe: str = "1m", _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    if timeframe not in {"1m", "1d"}:
        raise HTTPException(status_code=422, detail="仅支持 1m 或 1d")
    stock = db.scalar(select(Stock).where(Stock.symbol == symbol))
    if not stock:
        raise HTTPException(status_code=404, detail="股票不存在")
    cache_root = Path(__file__).resolve().parents[2] / "data" / "market"
    class GeneratedProvider:
        def bars(self, *, symbol: str, timeframe: str, start: str | None = None, end: str | None = None):
            return generated_bars(stock, 48 if timeframe == "1m" else 30)
    cache_path = refresh_bar_cache(cache_root, GeneratedProvider(), symbol=stock.symbol, timeframe=timeframe)
    return read_bar_cache(cache_path)


@app.get("/api/market-data/quotes")
def market_quotes(symbols: str, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    requested = [item.strip() for item in symbols.split(",") if item.strip()]
    rows = db.scalars(select(Stock).where(Stock.symbol.in_(requested)))
    current = now()
    result = []
    for stock in rows:
        quote_at = stock.quote_updated_at
        if quote_at and quote_at.tzinfo is None:
            quote_at = quote_at.replace(tzinfo=current.tzinfo)
        result.append({
            "symbol": stock.symbol,
            "last_price": stock.last_price,
            "bid_price": round((stock.last_price or 0) * 0.999, 3),
            "ask_price": round((stock.last_price or 0) * 1.001, 3),
            "volume": 1_200_000,
            "amount": stock.turnover_amount,
            "quote_at": quote_at.isoformat() if quote_at else None,
            "stale": quote_is_stale(quote_at, current=current, stale_after_seconds=settings.market_stale_seconds),
            "provider": settings.market_provider,
        })
    return result


@app.get("/api/market-data/realtime-status")
def realtime_status(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    current = now()
    rows = list(
        db.scalars(
            select(Stock).join(WatchlistItem, WatchlistItem.stock_id == Stock.id).order_by(WatchlistItem.id)
        )
    )
    return [
        {
            "symbol": stock.symbol,
            "name": stock.name,
            "quote_at": stock.quote_updated_at.isoformat() if stock.quote_updated_at else None,
            "stale": quote_is_stale(stock.quote_updated_at, current=current, stale_after_seconds=settings.market_stale_seconds),
        }
        for stock in rows
    ]


@app.post("/api/market-data/realtime-poll")
def realtime_poll(_: Administrator = Depends(require_admin)):
    result = poll_watchlist_quotes()
    return {
        **result,
        "interval_seconds": settings.realtime_poll_seconds,
        "stale_after_seconds": settings.market_stale_seconds,
    }


@app.get("/api/notifications/channels")
def notification_channels(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    return [serialize(item) for item in db.scalars(select(NotificationChannel).order_by(NotificationChannel.id))]


@app.post("/api/notifications/channels", status_code=201)
def create_notification(payload: NotificationCreate, _: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    if payload.type not in {"email", "wecom"}:
        raise HTTPException(status_code=422, detail="通知渠道类型无效")
    item = NotificationChannel(**payload.model_dump(), enabled=True)
    db.add(item)
    db.commit()
    db.refresh(item)
    return serialize(item)


@app.post("/api/notifications/channels/{channel_id}/test", status_code=202)
def test_notification(
    channel_id: int,
    background_tasks: BackgroundTasks,
    _: Administrator = Depends(require_admin),
    db: Session = Depends(get_db),
):
    channel = db.get(NotificationChannel, channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="通知渠道不存在")
    delivery = NotificationDelivery(
        channel_id=channel.id,
        event_type="daily_summary",
        severity="info",
        subject="GuPiao 通知测试",
        payload={"message": "通知渠道配置已进入投递队列"},
        status="pending" if channel.enabled else "failed",
        attempt_count=0,
        last_error=None if channel.enabled else "渠道未启用",
    )
    channel.last_tested_at = now()
    db.add(delivery)
    db.commit()
    db.refresh(delivery)
    if channel.enabled:
        background_tasks.add_task(_deliver_notification, delivery.id)
    return serialize(delivery)


@app.get("/api/notifications/deliveries")
def notification_deliveries(_: Administrator = Depends(require_admin), db: Session = Depends(get_db)):
    return [serialize(item) for item in db.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id.desc()).limit(200))]


def _deliver_notification(delivery_id: int) -> None:
    with SessionLocal() as db:
        delivery = db.get(NotificationDelivery, delivery_id)
        if not delivery:
            return
        channel = db.get(NotificationChannel, delivery.channel_id)
        if not channel or not channel.enabled:
            delivery.status = "failed"
            delivery.last_error = "通知渠道不存在或未启用"
            db.commit()
            return
        result = deliver_channel(
            settings,
            channel_type=channel.type,
            recipient=channel.recipient,
            secret_ref=channel.secret_ref,
            subject=delivery.subject,
            message=str(delivery.payload.get("message", delivery.payload)),
        )
        delivery.status = "sent" if result.sent else "failed"
        delivery.attempt_count = result.attempt_count
        delivery.last_error = result.last_error
        delivery.sent_at = now() if result.sent else None
        db.commit()
