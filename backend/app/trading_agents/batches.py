from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from ..models import (
    MarketDailyBar,
    DataSourceState,
    Position,
    RiskEvent,
    SimulationAccount,
    Stock,
    StockEvent,
    StrategyConfig,
    TradingAgentBatch,
    TradingAgentCandidateAnalysis,
    TradingAgentPortfolioDecision,
    now,
)
from .config import TRADING_AGENTS_DEFAULTS, configuration_fingerprint, data_root
from .enrichment import collect_enrichment
from .portfolio import map_target_weights
from .prefilter import build_snapshot, select_candidates
from .rebalance import revalue_simulation_account
from .runtime import simulation_account_is_available


BATCH_LEASE = timedelta(minutes=10)
PROMPT_VERSION = "1"
BLOCKING_EVENT_TYPES = {
    "suspension",
    "resumption",
    "regulatory_investigation",
    "material_litigation",
    "shareholder_reduction",
    "earnings_warning",
    "major_announcement",
}


@dataclass(frozen=True)
class AnalysisResult:
    rating: str
    ai_target_weight: float | None
    report: str
    reasoning: str
    llm_calls: int
    tokens_in: int
    tokens_out: int
    report_uri: str | None = None


class BatchAnalyzer(Protocol):
    def analyze(self, **kwargs: Any) -> AnalysisResult: ...

    def decide_portfolio(self, **kwargs: Any) -> dict[str, Any]: ...


def _aware(value: datetime, reference: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=reference.tzinfo)


def _wall_datetime(current: datetime, value: str) -> datetime:
    parsed = time.fromisoformat(value)
    return datetime.combine(current.date(), parsed, tzinfo=current.tzinfo)


def _critical_symbols(db: Session, *, current: datetime) -> set[str]:
    start = current - timedelta(days=7)
    rows = list(
        db.execute(
            select(Stock.symbol, StockEvent.event_type, StockEvent.unlock_free_float_pct)
            .join(StockEvent, StockEvent.stock_id == Stock.id)
            .where(
                StockEvent.published_at >= start,
                StockEvent.published_at <= current,
            )
        )
    )
    result: set[str] = set()
    for symbol, event_type, unlock_pct in rows:
        if event_type in BLOCKING_EVENT_TYPES:
            result.add(symbol)
        elif event_type == "unlock" and (unlock_pct is None or unlock_pct > 0.05):
            result.add(symbol)
    return result


def _snapshot_rows(db: Session, config: StrategyConfig, *, current: datetime) -> list[dict[str, Any]]:
    parameters = {**TRADING_AGENTS_DEFAULTS, **(config.parameters or {})}
    positions = {
        item.stock_id
        for item in db.scalars(
            select(Position).where(
                Position.account_id == config.simulation_account_id,
                Position.mode == "SIMULATION",
                Position.quantity > 0,
            )
        )
    }
    liquid_stocks = list(
        db.scalars(
            select(Stock)
            .where(
                Stock.status == "active",
                Stock.exchange.in_(["SSE", "SZSE"]),
            )
            .order_by(
                func.coalesce(Stock.turnover_amount, 0).desc(),
                Stock.symbol,
            )
            .limit(int(parameters["prefilter_size"]))
        )
    )
    stocks_by_id = {stock.id: stock for stock in liquid_stocks}
    if positions:
        for stock in db.scalars(select(Stock).where(Stock.id.in_(positions))):
            stocks_by_id[stock.id] = stock
    stocks = sorted(stocks_by_id.values(), key=lambda item: item.symbol)
    rows: list[dict[str, Any]] = []
    for stock in stocks:
        bars = list(
            db.scalars(
                select(MarketDailyBar)
                .where(
                    MarketDailyBar.stock_id == stock.id,
                    MarketDailyBar.trade_date <= current.date().isoformat(),
                )
                .order_by(MarketDailyBar.trade_date.desc())
                .limit(60)
            )
        )
        events = list(
            db.scalars(
                select(StockEvent)
                .where(
                    StockEvent.stock_id == stock.id,
                    StockEvent.published_at >= current - timedelta(days=7),
                    StockEvent.published_at <= current,
                )
                .order_by(StockEvent.published_at, StockEvent.id)
            )
        )
        rows.append(
            {
                "stock_id": stock.id,
                "symbol": stock.symbol,
                "name": stock.name,
                "exchange": stock.exchange,
                "status": stock.status,
                "last_price": stock.last_price,
                "change_pct": stock.change_pct,
                "turnover_amount": stock.turnover_amount,
                "quote_updated_at": stock.quote_updated_at,
                "is_holding": stock.id in positions,
                "bars": [
                    {
                        "trade_date": bar.trade_date,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                        "amount": bar.amount,
                        "source": bar.source,
                    }
                    for bar in reversed(bars)
                ],
                "events": [
                    {
                        "event_type": event.event_type,
                        "severity": event.severity,
                        "title": event.title,
                        "source": event.source,
                        "published_at": event.published_at.isoformat(),
                        "raw_uri": event.raw_uri,
                    }
                    for event in events
                ],
            }
        )
    return rows


def _validate_event_source(
    db: Session,
    *,
    current: datetime,
    max_age_seconds: int,
) -> None:
    sources = [
        source
        for source in db.scalars(select(DataSourceState).where(DataSourceState.enabled.is_(True)))
        if "corporate_events" in (source.capabilities or [])
        and source.healthy
        and source.last_checked_at is not None
    ]
    if not sources:
        raise ValueError("公司公告数据源不可用")
    latest = max(sources, key=lambda source: source.last_checked_at)
    checked_at = _aware(latest.last_checked_at, current)
    age = (current - checked_at).total_seconds()
    if age < 0 or age > max_age_seconds:
        raise ValueError("公司公告数据已过期")


def _validate_required_snapshots(
    rows: dict[str, dict[str, Any]],
    required_symbols: list[str],
    *,
    current: datetime,
    quote_max_age_seconds: int,
    daily_max_age_days: int,
) -> None:
    for symbol in required_symbols:
        row = rows.get(symbol)
        if not row:
            raise ValueError(f"{symbol} 核心快照缺失")
        quote_at = row.get("quote_updated_at")
        if quote_at is None:
            raise ValueError(f"{symbol} 实时行情缺失")
        quote_at = _aware(quote_at, current)
        age = (current - quote_at).total_seconds()
        if age < 0 or age > quote_max_age_seconds:
            raise ValueError(f"{symbol} 实时行情已过期")
        bars = row.get("bars") or []
        if len(bars) < 60:
            raise ValueError(f"{symbol} 日线不足60根")
        latest = datetime.fromisoformat(str(bars[-1]["trade_date"])).date()
        if (current.date() - latest).days > daily_max_age_days:
            raise ValueError(f"{symbol} 日线已过期")


def create_batch(
    db: Session,
    config: StrategyConfig,
    *,
    current: datetime | None = None,
    snapshot_root: Path | None = None,
) -> TradingAgentBatch:
    current = current or now()
    if not config.enabled:
        raise ValueError("TradingAgents 策略配置已停用")
    if config.mode != "SIMULATION" or not simulation_account_is_available(
        db,
        account_id=config.simulation_account_id,
        strategy_config_id=config.id,
    ):
        raise ValueError("TradingAgents 策略必须绑定未被其他策略配置占用的模拟账户")
    existing = db.scalar(
        select(TradingAgentBatch).where(
            TradingAgentBatch.strategy_config_id == config.id,
            TradingAgentBatch.trading_date == current.date().isoformat(),
        )
    )
    if existing:
        return existing
    parameters = {**TRADING_AGENTS_DEFAULTS, **(config.parameters or {})}
    _validate_event_source(
        db,
        current=current,
        max_age_seconds=int(parameters["event_max_age_seconds"]),
    )
    rows = _snapshot_rows(db, config, current=current)
    selection = select_candidates(
        rows,
        as_of=current.date().isoformat(),
        prefilter_size=int(parameters["prefilter_size"]),
        top_n=int(parameters["top_n"]),
        critical_event_symbols=_critical_symbols(db, current=current),
    )
    if len(selection.candidates) < int(parameters["top_n"]):
        raise ValueError("符合条件且具备60根日线的候选不足")
    by_symbol = {str(row["symbol"]): row for row in rows}
    _validate_required_snapshots(
        by_symbol,
        selection.required_symbols,
        current=current,
        quote_max_age_seconds=int(parameters["snapshot_quote_max_age_seconds"]),
        daily_max_age_days=int(parameters["daily_max_age_days"]),
    )
    snapshot_value = {
        "captured_at": current.isoformat(),
        "trading_date": current.date().isoformat(),
        "source": "gupiao",
        "candidates": [
            {
                **selection.candidates[index],
                "rank": index + 1,
            }
            for index in range(len(selection.candidates))
        ],
        "holdings": [by_symbol[symbol] for symbol in selection.holdings],
        "rejected": selection.rejected,
    }
    snapshot = build_snapshot(snapshot_value)
    root = snapshot_root or data_root()
    target = root / current.date().isoformat() / f"strategy-{config.id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(snapshot.payload, encoding="utf-8")
    batch = TradingAgentBatch(
        strategy_config_id=config.id,
        simulation_account_id=config.simulation_account_id,
        trading_date=current.date().isoformat(),
        status="pending",
        analysis_profile=str(parameters["analysis_profile"]),
        position_mapping=str(parameters["position_mapping"]),
        quick_model=str(parameters["quick_model"]),
        deep_model=str(parameters["deep_model"]),
        prompt_version=PROMPT_VERSION,
        config_fingerprint=configuration_fingerprint(
            parameters,
            simulation_account_id=config.simulation_account_id,
        ),
        candidate_symbols=[str(item["symbol"]) for item in selection.candidates],
        holding_symbols=selection.holdings,
        required_symbols=selection.required_symbols,
        snapshot_sha256=snapshot.sha256,
        snapshot_uri=str(target),
        analysis_deadline=_wall_datetime(current, str(parameters["analysis_deadline"])),
        rebalance_after=_wall_datetime(current, str(parameters["rebalance_time"])),
    )
    db.add(batch)
    db.flush()
    rank_by_symbol = {
        symbol: index + 1 for index, symbol in enumerate(batch.candidate_symbols)
    }
    for symbol in batch.required_symbols:
        stock = db.scalar(select(Stock).where(Stock.symbol == symbol))
        db.add(
            TradingAgentCandidateAnalysis(
                batch_id=batch.id,
                stock_id=stock.id,
                rank=rank_by_symbol.get(symbol),
                is_holding=symbol in batch.holding_symbols,
                status="pending",
            )
        )
    db.commit()
    db.refresh(batch)
    return batch


def claim_pending_batch(
    db: Session,
    *,
    worker_id: str,
    current: datetime | None = None,
) -> TradingAgentBatch | None:
    current = current or now()
    batch_id = db.scalar(
        select(TradingAgentBatch.id)
        .where(
            or_(
                TradingAgentBatch.status == "pending",
                and_(
                    TradingAgentBatch.status == "processing",
                    TradingAgentBatch.lease_until.is_not(None),
                    TradingAgentBatch.lease_until <= current,
                ),
            )
        )
        .order_by(TradingAgentBatch.id)
        .limit(1)
    )
    if batch_id is None:
        return None
    result = db.execute(
        update(TradingAgentBatch)
        .where(
            TradingAgentBatch.id == batch_id,
            or_(
                TradingAgentBatch.status == "pending",
                and_(
                    TradingAgentBatch.status == "processing",
                    TradingAgentBatch.lease_until <= current,
                ),
            ),
        )
        .values(
            status="processing",
            worker_id=worker_id,
            lease_until=current + BATCH_LEASE,
            started_at=func.coalesce(TradingAgentBatch.started_at, current),
            updated_at=current,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if result.rowcount != 1:
        return None
    return db.get(TradingAgentBatch, batch_id)


def _fail_batch(db: Session, batch: TradingAgentBatch, message: str) -> TradingAgentBatch:
    batch.status = "failed"
    batch.error_message = message[:2000]
    batch.completed_at = now()
    batch.lease_until = None
    db.add(
        RiskEvent(
            mode="SIMULATION",
            event_type="trading_agents_batch_failure",
            message=message[:2000],
            context={
                "batch_id": batch.id,
                "simulation_account_id": batch.simulation_account_id,
            },
        )
    )
    db.commit()
    return batch


def _budget_exceeded(batch: TradingAgentBatch, parameters: dict[str, Any]) -> bool:
    return (
        batch.llm_calls > int(parameters["max_llm_calls"])
        or batch.tokens_in > int(parameters["max_input_tokens"])
        or batch.tokens_out > int(parameters["max_output_tokens"])
    )


def process_batch(
    db: Session,
    batch: TradingAgentBatch,
    *,
    analyzer: BatchAnalyzer,
    current: datetime | None = None,
    enrichment_collector=collect_enrichment,
) -> TradingAgentBatch:
    use_wall_clock = current is None
    current = current or now()
    deadline = _aware(batch.analysis_deadline, current)
    if current > deadline:
        return _fail_batch(db, batch, "已超过分析截止时间")
    config = db.get(StrategyConfig, batch.strategy_config_id)
    parameters = {**TRADING_AGENTS_DEFAULTS, **(config.parameters or {})}
    current_fingerprint = configuration_fingerprint(
        parameters,
        simulation_account_id=config.simulation_account_id,
    )
    if not batch.config_fingerprint or batch.config_fingerprint != current_fingerprint:
        return _fail_batch(db, batch, "策略配置或模拟账户在建批后发生变化")
    snapshot_path = Path(batch.snapshot_uri)
    snapshot_text = snapshot_path.read_text(encoding="utf-8")
    if build_snapshot(json.loads(snapshot_text)).sha256 != batch.snapshot_sha256:
        return _fail_batch(db, batch, "批次快照 SHA-256 哈希校验失败")
    snapshot = json.loads(snapshot_text)
    if parameters["enrichment_enabled"] and "enrichment" not in snapshot:
        snapshot["enrichment"] = enrichment_collector(
            batch.required_symbols,
            trading_date=batch.trading_date,
            concurrency=int(parameters["worker_concurrency"]),
            timeout_seconds=int(parameters["enrichment_timeout_seconds"]),
        )
        enriched = build_snapshot(snapshot)
        enriched_path = snapshot_path.with_name(
            f"{snapshot_path.stem}-enriched{snapshot_path.suffix}"
        )
        temporary = enriched_path.with_suffix(enriched_path.suffix + ".tmp")
        temporary.write_text(enriched.payload, encoding="utf-8")
        temporary.replace(enriched_path)
        batch.snapshot_uri = str(enriched_path)
        batch.snapshot_sha256 = enriched.sha256
        batch.lease_until = now() + BATCH_LEASE
        db.commit()
        check_time = now() if use_wall_clock else current
        if check_time > _aware(deadline, check_time):
            return _fail_batch(db, batch, "补充数据固化后已超过分析截止时间")
    snapshot_by_symbol = {
        str(item["symbol"]): item
        for item in snapshot["candidates"] + snapshot["holdings"]
    }
    items = list(
        db.scalars(
            select(TradingAgentCandidateAnalysis)
            .where(TradingAgentCandidateAnalysis.batch_id == batch.id)
            .order_by(
                TradingAgentCandidateAnalysis.rank.is_(None),
                TradingAgentCandidateAnalysis.rank,
                TradingAgentCandidateAnalysis.stock_id,
            )
        )
    )
    completed: list[dict[str, Any]] = []
    pending: list[tuple[int, str]] = []
    for item in items:
        stock = db.get(Stock, item.stock_id)
        if item.status == "completed":
            completed.append(
                {
                    "symbol": stock.symbol,
                    "rating": item.rating,
                    "ai_target_weight": item.ai_target_weight,
                    "rank": item.rank or 9999,
                    "is_holding": item.is_holding,
                    "reasoning": item.reasoning or "",
                }
            )
            continue
        item.status = "processing"
        item.started_at = current
        item.error_message = None
        pending.append((item.id, stock.symbol))
    db.commit()

    trading_date = batch.trading_date
    analysis_profile = batch.analysis_profile
    quick_model = batch.quick_model
    deep_model = batch.deep_model

    def analyze(symbol: str) -> AnalysisResult:
        reference = now() if use_wall_clock else current
        remaining_seconds = max(
            1,
            int((_aware(deadline, reference) - reference).total_seconds()),
        )
        enrichment = snapshot.get("enrichment") or {}
        candidate_snapshot = {
            **snapshot_by_symbol[symbol],
            "enrichment": {
                "source": enrichment.get("source"),
                "captured_at": enrichment.get("captured_at"),
                "symbol": (enrichment.get("symbols") or {}).get(symbol, {}),
                "global": enrichment.get("global") or {},
            },
        }
        return analyzer.analyze(
            symbol=symbol,
            trading_date=trading_date,
            snapshot=candidate_snapshot,
            profile=analysis_profile,
            quick_model=quick_model,
            deep_model=deep_model,
            timeout_seconds=min(
                int(parameters["candidate_timeout_seconds"]),
                remaining_seconds,
            ),
        )

    maximum_workers = max(1, int(parameters["worker_concurrency"]))
    futures: dict[Future[AnalysisResult], tuple[int, str]] = {}
    with ThreadPoolExecutor(max_workers=maximum_workers) as executor:
        for item_id, symbol in pending:
            futures[executor.submit(analyze, symbol)] = (item_id, symbol)
        failure: tuple[int, str, Exception] | None = None
        for future in as_completed(futures):
            item_id, symbol = futures[future]
            if failure:
                continue
            try:
                result = future.result()
            except Exception as exc:
                failure = (item_id, symbol, exc)
                for candidate_future in futures:
                    if candidate_future is not future:
                        candidate_future.cancel()
                continue
            db.refresh(batch)
            if batch.status == "cancelled":
                return batch
            check_time = now() if use_wall_clock else current
            if check_time > _aware(batch.analysis_deadline, check_time):
                failure = (item_id, symbol, RuntimeError("已超过分析截止时间"))
                for candidate_future in futures:
                    if candidate_future is not future:
                        candidate_future.cancel()
                continue
            item = db.get(TradingAgentCandidateAnalysis, item_id)
            item.status = "completed"
            item.rating = result.rating
            item.ai_target_weight = result.ai_target_weight
            item.report_uri = result.report_uri
            item.report = result.report
            item.reasoning = result.reasoning
            item.stats = {
                "llm_calls": result.llm_calls,
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
            }
            item.finished_at = now()
            batch.llm_calls += result.llm_calls
            batch.tokens_in += result.tokens_in
            batch.tokens_out += result.tokens_out
            batch.lease_until = now() + BATCH_LEASE
            db.commit()
            if _budget_exceeded(batch, parameters):
                failure = (item.id, symbol, RuntimeError("TradingAgents 每日预算已超限"))
                for candidate_future in futures:
                    if candidate_future is not future:
                        candidate_future.cancel()
                continue
            completed.append(
                {
                    "symbol": symbol,
                    "rating": result.rating,
                    "ai_target_weight": result.ai_target_weight,
                    "rank": item.rank or 9999,
                    "is_holding": item.is_holding,
                    "reasoning": result.reasoning,
                }
            )
    if failure:
        db.refresh(batch)
        if batch.status == "cancelled":
            return batch
        item_id, symbol, exc = failure
        item = db.get(TradingAgentCandidateAnalysis, item_id)
        item.status = "failed"
        item.error_message = str(exc)[:2000]
        item.finished_at = now()
        db.commit()
        if "预算" in str(exc):
            return _fail_batch(db, batch, str(exc))
        return _fail_batch(db, batch, f"{symbol}: {exc}")

    completed.sort(key=lambda item: (int(item["rank"]), str(item["symbol"])))
    db.refresh(batch)
    if batch.status == "cancelled":
        return batch
    try:
        portfolio_reference = now() if use_wall_clock else current
        portfolio_timeout = int(
            (_aware(batch.analysis_deadline, portfolio_reference) - portfolio_reference)
            .total_seconds()
        )
        if portfolio_timeout <= 0:
            raise TimeoutError("已超过分析截止时间")
        portfolio = analyzer.decide_portfolio(
            analyses=completed,
            trading_date=batch.trading_date,
            profile=batch.analysis_profile,
            quick_model=batch.quick_model,
            deep_model=batch.deep_model,
            timeout_seconds=min(240, portfolio_timeout),
        )
        portfolio_by_symbol = {
            str(item["symbol"]): item
            for item in portfolio.get("rankings", [])
        }
        if set(portfolio_by_symbol) != {item["symbol"] for item in completed}:
            raise ValueError("跨股票组合决策未覆盖全部股票")
        for item in completed:
            portfolio_item = portfolio_by_symbol[item["symbol"]]
            item["rank"] = int(portfolio_item["rank"])
            if "ai_target_weight" in portfolio_item:
                item["ai_target_weight"] = portfolio_item["ai_target_weight"]
        batch.llm_calls += int(portfolio.get("llm_calls", 0))
        batch.tokens_in += int(portfolio.get("tokens_in", 0))
        batch.tokens_out += int(portfolio.get("tokens_out", 0))
        if _budget_exceeded(batch, parameters):
            return _fail_batch(db, batch, "TradingAgents 每日预算已超限")
        account = db.get(SimulationAccount, batch.simulation_account_id)
        revalue_simulation_account(db, account)
        total_asset = float(account.total_asset)
        current_weights: dict[str, float] = {}
        for position, symbol in db.execute(
            select(Position, Stock.symbol)
            .join(Stock, Stock.id == Position.stock_id)
            .where(
                Position.account_id == batch.simulation_account_id,
                Position.mode == "SIMULATION",
                Position.quantity > 0,
            )
        ):
            current_weights[symbol] = float(position.market_value) / total_asset if total_asset else 0
        targets = map_target_weights(
            analyses=completed,
            current_weights=current_weights,
            mode=batch.position_mapping,
            max_positions=int(parameters["max_positions"]),
            max_position_pct=float(parameters["max_position_pct"]),
            max_total_exposure_pct=float(parameters["max_total_exposure_pct"]),
        )
    except Exception as exc:
        return _fail_batch(db, batch, f"组合决策失败: {exc}")
    db.add(
        TradingAgentPortfolioDecision(
            batch_id=batch.id,
            status="ready",
            position_mapping=batch.position_mapping,
            target_weights=targets,
            rankings=portfolio.get("rankings", []),
            rationale=str(portfolio.get("rationale", "")),
            model=batch.deep_model,
            llm_calls=int(portfolio.get("llm_calls", 0)),
            tokens_in=int(portfolio.get("tokens_in", 0)),
            tokens_out=int(portfolio.get("tokens_out", 0)),
        )
    )
    batch.status = "ready"
    batch.completed_at = now()
    batch.lease_until = None
    db.commit()
    return batch
