from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    FinancialReportSnapshot,
    MarketDailyBar,
    MarketDailyMetric,
    QuantCandidateScore,
    QuantPortfolioDecision,
    Position,
    SimulationAccount,
    Stock,
    StockEvent,
    StrategyConfig,
    StrategyDefinition,
    StrategyPositionLot,
)
from .algorithms import (
    CandidateInput,
    FinancialPoint,
    PriceBar,
    build_target_portfolio,
)
from .catalog import DEFAULT_ETF_UNIVERSE, QUANT_STRATEGY_SPECS
from .readiness import (
    configuration_fingerprint,
    corporate_event_data_reason,
    quant_dataset_state_reasons,
)
from .holding_policy import HoldingContext, apply_holding_policy


class DataNotReadyError(RuntimeError):
    pass


BLOCKING_EVENTS = {
    "suspension",
    "regulatory_investigation",
    "material_litigation",
    "shareholder_reduction",
    "earnings_warning",
    "major_announcement",
}

DATA_REJECTION_PREFIXES = (
    "缺少",
    "已完成复权日线不足",
    "点时财务字段不完整",
    "财务数据在决策时尚不可见",
    "估值指标无效",
    "基准日线不足",
    "ETF日线不足",
    "风险平价权重未收敛",
    "ATR无效",
)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def _bars(
    db: Session,
    stock_id: int,
    as_of: date,
    limit: int = 300,
    *,
    require_adjusted: bool = False,
) -> tuple[PriceBar, ...]:
    conditions = [
        MarketDailyBar.stock_id == stock_id,
        MarketDailyBar.trade_date <= as_of.isoformat(),
        MarketDailyBar.quality_status == "valid",
        ~func.lower(MarketDailyBar.source).like("%demo%"),
    ]
    if require_adjusted:
        conditions.extend(
            [
                MarketDailyBar.adjusted_close.is_not(None),
                MarketDailyBar.adjustment_factor.is_not(None),
                MarketDailyBar.adjustment_factor > 0,
            ]
        )
    rows = list(
        db.scalars(
            select(MarketDailyBar)
            .where(*conditions)
            .order_by(MarketDailyBar.trade_date.desc())
            .limit(limit)
        )
    )
    rows.reverse()
    if rows and rows[-1].trade_date != as_of.isoformat():
        return ()
    return tuple(
        PriceBar(
            trade_date=_parse_date(row.trade_date),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
            amount=float(row.amount),
            adjusted_close=float(row.adjusted_close or row.close),
        )
        for row in rows
    )


def _financial_rows(
    db: Session,
    stock_id: int,
    as_of: date,
) -> list[FinancialPoint]:
    rows = list(
        db.scalars(
            select(FinancialReportSnapshot)
            .where(
                FinancialReportSnapshot.stock_id == stock_id,
                FinancialReportSnapshot.available_on <= as_of.isoformat(),
                FinancialReportSnapshot.actual_announcement_date <= as_of.isoformat(),
            )
            .order_by(FinancialReportSnapshot.report_period)
        )
    )
    latest_by_period: dict[str, FinancialReportSnapshot] = {}
    for row in rows:
        latest_by_period[row.report_period] = row
    return [
        FinancialPoint(
            report_period=_parse_date(row.report_period),
            actual_announcement_date=_parse_date(row.actual_announcement_date),
            available_on=_parse_date(row.available_on),
            eps=row.eps,
            roe=row.roe,
            gross_margin=row.gross_margin,
            operating_cash_flow=row.operating_cash_flow,
            net_profit=row.net_profit,
            revenue=row.revenue,
            total_assets=row.total_assets,
            total_liabilities=row.total_liabilities,
        )
        for row in latest_by_period.values()
    ]


def _metric(db: Session, stock_id: int, as_of: date) -> dict[str, float]:
    row = db.scalar(
        select(MarketDailyMetric)
        .where(
            MarketDailyMetric.stock_id == stock_id,
            MarketDailyMetric.trade_date <= as_of.isoformat(),
        )
        .order_by(MarketDailyMetric.trade_date.desc())
        .limit(1)
    )
    if row is None:
        return {}
    return {
        key: float(value)
        for key in ("pe_ttm", "pb", "dividend_yield", "total_market_value", "float_market_value")
        if (value := getattr(row, key)) is not None
    }


def _candidate(db: Session, stock: Stock, as_of: date) -> CandidateInput:
    financial = _financial_rows(db, stock.id, as_of)
    return CandidateInput(
        symbol=stock.symbol,
        name=stock.name,
        instrument_type=stock.instrument_type,
        bars=_bars(
            db,
            stock.id,
            as_of,
            require_adjusted=stock.instrument_type == "STOCK",
        ),
        financial=financial[-1] if financial else None,
        metric=_metric(db, stock.id, as_of),
        financial_history=tuple(financial[:-1]),
    )


def _universe(
    db: Session,
    config: StrategyConfig,
    key: str,
    as_of: date,
    *,
    decision_at: datetime | None = None,
) -> tuple[list[Stock], dict[str, tuple[str, ...]]]:
    spec = QUANT_STRATEGY_SPECS[key]
    parameters = config.parameters or {}
    if spec.asset_type == "ETF":
        symbols = list(parameters.get("etf_universe") or DEFAULT_ETF_UNIVERSE)
        rows = list(
            db.scalars(
                select(Stock)
                .where(
                    Stock.symbol.in_(symbols),
                    Stock.status == "active",
                    Stock.instrument_type == "ETF",
                )
                .order_by(Stock.symbol)
            )
        )
        return rows, {}

    prefilter_size = min(800, max(1, int(parameters.get("prefilter_size", 800))))
    rows = list(
        db.scalars(
            select(Stock)
            .where(
                Stock.status == "active",
                Stock.exchange.in_(["SSE", "SZSE"]),
                Stock.instrument_type == "STOCK",
            )
            .order_by(Stock.symbol)
        )
    )
    blocked: dict[str, tuple[str, ...]] = {}
    eligible: list[tuple[Stock, float]] = []
    for stock in rows:
        reasons = []
        if "ST" in stock.name.upper():
            reasons.append("ST股票")
        if stock.listing_date is None:
            reasons.append("缺少上市日期")
        elif (as_of - _parse_date(stock.listing_date)).days < 120:
            reasons.append("上市不足120日")
        cutoff = decision_at or datetime.combine(as_of, datetime.max.time())
        event = db.scalar(
            select(StockEvent.id).where(
                StockEvent.stock_id == stock.id,
                StockEvent.event_type.in_(BLOCKING_EVENTS),
                StockEvent.published_at >= cutoff - timedelta(days=7),
                StockEvent.published_at <= cutoff,
            )
        )
        if event:
            reasons.append("命中风险公告")
        amounts = list(
            db.scalars(
                select(MarketDailyBar.amount)
                .where(
                    MarketDailyBar.stock_id == stock.id,
                    MarketDailyBar.trade_date <= as_of.isoformat(),
                )
                .order_by(MarketDailyBar.trade_date.desc())
                .limit(20)
            )
        )
        if len(amounts) < 20:
            reasons.append("20日成交额历史不足")
        average_amount = sum(float(value or 0) for value in amounts) / len(amounts) if amounts else 0
        if average_amount < float(parameters.get("min_average_turnover", 100_000_000)):
            reasons.append("20日平均成交额不足1亿元")
        if reasons:
            blocked[stock.symbol] = tuple(dict.fromkeys(reasons))
        else:
            eligible.append((stock, average_amount))
    eligible.sort(key=lambda row: (-row[1], row[0].symbol))
    return [row[0] for row in eligible[:prefilter_size]], blocked


def _benchmark(db: Session, config: StrategyConfig, key: str, as_of: date) -> CandidateInput | None:
    if key not in {"short_term_reversal_t1", "regime_allocator"}:
        return None
    symbol = str((config.parameters or {}).get("benchmark_symbol", "000300.SH"))
    stock = db.scalar(select(Stock).where(Stock.symbol == symbol))
    if stock is None:
        return None
    return _candidate(db, stock, as_of)


def _snapshot_value(
    key: str,
    as_of: date,
    result,
    *,
    db: Session,
    stocks: list[Stock],
) -> dict:
    inputs = {}
    for stock in stocks:
        rows = list(
            db.scalars(
                select(MarketDailyBar).where(
                    MarketDailyBar.stock_id == stock.id,
                    MarketDailyBar.trade_date <= as_of.isoformat(),
                    MarketDailyBar.quality_status == "valid",
                )
            )
        )
        inputs[stock.symbol] = {
            "bar_count": len(rows),
            "latest_trade_date": max(
                (row.trade_date for row in rows),
                default=None,
            ),
            "sources": sorted({row.source for row in rows}),
        }
    return {
        "strategy_key": key,
        "as_of": as_of.isoformat(),
        "target_weights": result.target_weights,
        "scores": result.scores,
        "features": result.features,
        "rejected": result.rejected,
        "metadata": result.metadata,
        "inputs": inputs,
    }


def _holding_contexts(
    db: Session,
    config: StrategyConfig,
    as_of: date,
    *,
    decision_at: datetime | None = None,
) -> list[HoldingContext]:
    account = db.get(SimulationAccount, config.simulation_account_id)
    if account is None:
        return []
    holding_rows: list[tuple[Position, Stock, tuple[PriceBar, ...]]] = []
    for position, stock in db.execute(
        select(Position, Stock)
        .join(Stock, Stock.id == Position.stock_id)
        .where(
            Position.account_id == account.id,
            Position.mode == "SIMULATION",
            Position.quantity > 0,
        )
    ):
        bars = _bars(
            db,
            stock.id,
            as_of,
            require_adjusted=stock.instrument_type == "STOCK",
        )
        if bars:
            holding_rows.append((position, stock, bars))
    total_asset = float(account.cash_balance or 0) + sum(
        int(position.quantity) * float(bars[-1].close)
        for position, _stock, bars in holding_rows
    )
    if total_asset <= 0:
        return []
    contexts: list[HoldingContext] = []
    cutoff = decision_at or datetime.combine(as_of, datetime.max.time())
    for position, stock, bars in holding_rows:
        lot = db.scalar(
            select(StrategyPositionLot)
            .where(
                StrategyPositionLot.strategy_config_id == config.id,
                StrategyPositionLot.account_id == account.id,
                StrategyPositionLot.stock_id == stock.id,
                StrategyPositionLot.status == "open",
                StrategyPositionLot.remaining_quantity > 0,
            )
            .order_by(StrategyPositionLot.id)
            .limit(1)
        )
        metadata = lot.strategy_metadata if lot else {}
        entry_date = _parse_date(
            str(metadata.get("entry_date") or bars[0].trade_date.isoformat())
        )
        held_days = sum(1 for bar in bars if bar.trade_date > entry_date)
        latest_close = bars[-1].close
        prior_window = bars[-21:-1] or bars[-20:]
        low_20d = min(bar.low for bar in prior_window)
        highest_close = max(
            bar.close for bar in bars if bar.trade_date >= entry_date
        )
        entry_atr = float(metadata.get("entry_atr") or 0)
        contexts.append(
            HoldingContext(
                symbol=stock.symbol,
                current_weight=(
                    int(position.quantity) * latest_close / total_asset
                ),
                entry_date=entry_date,
                held_trading_days=held_days,
                latest_close=latest_close,
                low_20d=low_20d,
                highest_close=highest_close,
                entry_atr=entry_atr,
                risk_blocked=(
                    stock.status != "active"
                    or "ST" in stock.name.upper()
                    or db.scalar(
                        select(StockEvent.id).where(
                            StockEvent.stock_id == stock.id,
                            StockEvent.event_type.in_(BLOCKING_EVENTS),
                            StockEvent.published_at >= cutoff - timedelta(days=7),
                            StockEvent.published_at <= cutoff,
                        )
                    )
                    is not None
                ),
            )
        )
    return contexts


def _consumed_reports(db: Session, config: StrategyConfig) -> set[tuple[str, str]]:
    rows = db.execute(
        select(StrategyPositionLot, Stock)
        .join(Stock, Stock.id == StrategyPositionLot.stock_id)
        .where(StrategyPositionLot.strategy_config_id == config.id)
    )
    return {
        (stock.symbol, str(lot.strategy_metadata.get("report_period")))
        for lot, stock in rows
        if lot.strategy_metadata and lot.strategy_metadata.get("report_period")
    }


def build_signal_decision(
    db: Session,
    config: StrategyConfig,
    *,
    current: datetime,
    as_of: date | None = None,
    decision_type: str = "signal",
) -> QuantPortfolioDecision:
    definition = db.get(StrategyDefinition, config.strategy_definition_id)
    if definition is None or definition.key not in QUANT_STRATEGY_SPECS:
        raise ValueError("配置不属于独立量化策略")
    if config.mode != "SIMULATION" or not config.simulation_account_id:
        raise ValueError("独立量化信号必须绑定模拟账户")
    if decision_type not in {"signal", "dry_run"}:
        raise ValueError("独立量化决策类型无效")
    as_of = as_of or current.date()
    config_fingerprint = configuration_fingerprint(
        config.parameters or {},
        simulation_account_id=config.simulation_account_id,
        strategy_version=definition.version,
    )
    existing = db.scalar(
        select(QuantPortfolioDecision).where(
            QuantPortfolioDecision.strategy_config_id == config.id,
            QuantPortfolioDecision.trading_date == as_of.isoformat(),
            QuantPortfolioDecision.decision_type == decision_type,
            QuantPortfolioDecision.config_fingerprint == config_fingerprint,
        )
    )
    if existing:
        return existing
    batch_reasons = quant_dataset_state_reasons(
        db,
        definition.key,
        as_of=as_of,
    )
    if batch_reasons:
        raise DataNotReadyError("；".join(batch_reasons))
    if "events" in QUANT_STRATEGY_SPECS[definition.key].required_datasets:
        event_reason = corporate_event_data_reason(db, current=current)
        if event_reason:
            raise DataNotReadyError(event_reason)
    stocks, prefilter_rejected = _universe(
        db,
        config,
        definition.key,
        as_of,
        decision_at=current,
    )
    candidates = [_candidate(db, stock, as_of) for stock in stocks]
    result = build_target_portfolio(
        definition.key,
        candidates,
        benchmark=_benchmark(db, config, definition.key, as_of),
        as_of=as_of,
        parameters=config.parameters or {},
    )
    result = apply_holding_policy(
        result,
        holdings=_holding_contexts(db, config, as_of, decision_at=current),
        consumed_reports=_consumed_reports(db, config),
        parameters=config.parameters or {},
    )
    rejected = {**prefilter_rejected, **result.rejected}
    if not candidates:
        detail = next(iter(prefilter_rejected.values()), ("数据不完整",))[0]
        raise DataNotReadyError(f"没有满足数据要求的候选: {detail}")
    if not result.target_weights and not result.scores:
        candidate_reasons = [
            reason
            for item in candidates
            for reason in result.rejected.get(item.symbol, ())
        ]
        if candidate_reasons and all(
            reason.startswith(DATA_REJECTION_PREFIXES)
            for reason in candidate_reasons
        ):
            raise DataNotReadyError(
                f"没有满足数据要求的候选: {candidate_reasons[0]}"
            )
    snapshot = _snapshot_value(
        definition.key,
        as_of,
        result,
        db=db,
        stocks=stocks,
    )
    snapshot_sha256 = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    decision = QuantPortfolioDecision(
        strategy_config_id=config.id,
        simulation_account_id=config.simulation_account_id,
        trading_date=as_of.isoformat(),
        decision_type=decision_type,
        status="ready",
        data_as_of=current,
        snapshot_sha256=snapshot_sha256,
        snapshot_payload=snapshot,
        config_fingerprint=config_fingerprint,
        strategy_version=definition.version,
        data_version=str((config.parameters or {}).get("data_version", "1")),
        target_weights=result.target_weights,
    )
    db.add(decision)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return db.scalar(
            select(QuantPortfolioDecision).where(
                QuantPortfolioDecision.strategy_config_id == config.id,
                QuantPortfolioDecision.trading_date == as_of.isoformat(),
                QuantPortfolioDecision.decision_type == decision_type,
                QuantPortfolioDecision.config_fingerprint == config_fingerprint,
            )
        )
    stock_by_symbol = {
        stock.symbol: stock
        for stock in db.scalars(
            select(Stock).where(
                Stock.symbol.in_(set(result.scores) | set(rejected))
            )
        )
    }
    ordered = sorted(result.scores.items(), key=lambda row: (-row[1], row[0]))
    for rank, (symbol, score) in enumerate(ordered, start=1):
        stock = stock_by_symbol[symbol]
        db.add(
            QuantCandidateScore(
                decision_id=decision.id,
                stock_id=stock.id,
                status="selected" if symbol in result.target_weights else "ranked",
                rank=rank,
                features=result.features.get(symbol, {}),
                score=score,
                target_weight=result.target_weights.get(symbol),
                rejection_reasons=[],
            )
        )
    for symbol, reasons in sorted(rejected.items()):
        stock = stock_by_symbol.get(symbol)
        if stock is None or symbol in result.scores:
            continue
        db.add(
            QuantCandidateScore(
                decision_id=decision.id,
                stock_id=stock.id,
                status="rejected",
                features=result.features.get(symbol, {}),
                rejection_reasons=list(reasons),
            )
        )
    db.commit()
    db.refresh(decision)
    return decision
