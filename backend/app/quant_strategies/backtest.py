from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import math
import statistics
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    BacktestRun,
    MarketDailyBar,
    SimulationAccount,
    Stock,
    StockEvent,
    StrategyBacktestQualification,
    StrategyConfig,
    StrategyDefinition,
)
from .algorithms import TargetPortfolio, build_target_portfolio
from .catalog import QUANT_STRATEGY_SPECS
from .holding_policy import HoldingContext, apply_holding_policy
from .readiness import configuration_fingerprint
from .signals import BLOCKING_EVENTS, _benchmark, _candidate, _universe


@dataclass(frozen=True)
class BacktestMetrics:
    trading_days: int
    data_completeness: float
    annualized_return: float
    sharpe_ratio: float
    max_drawdown: float
    trade_count: int
    final_equity: float
    equity_curve: tuple[dict[str, object], ...]


def qualification_passes(metrics: BacktestMetrics) -> bool:
    return (
        metrics.trading_days >= 500
        and metrics.data_completeness >= 0.98
        and metrics.annualized_return > 0
        and metrics.sharpe_ratio >= 0.30
        and metrics.max_drawdown >= -0.25
        and metrics.trade_count >= 30
    )


def _schedule_matches(frequency: str, dates: list[str], index: int) -> bool:
    current = date.fromisoformat(dates[index])
    if frequency == "daily" or frequency == "event":
        return True
    if frequency == "weekly":
        return index == len(dates) - 1 or date.fromisoformat(dates[index + 1]).isocalendar().week != current.isocalendar().week
    return index == len(dates) - 1 or (
        date.fromisoformat(dates[index + 1]).year,
        date.fromisoformat(dates[index + 1]).month,
    ) != (current.year, current.month)


def build_historical_target_portfolio(
    db: Session,
    config: StrategyConfig,
    *,
    as_of: date,
) -> TargetPortfolio:
    definition = db.get(StrategyDefinition, config.strategy_definition_id)
    stocks, _ = _universe(
        db,
        config,
        definition.key,
        as_of,
        decision_at=datetime.combine(as_of, time(16, 30)),
    )
    return build_target_portfolio(
        definition.key,
        [_candidate(db, stock, as_of) for stock in stocks],
        benchmark=_benchmark(db, config, definition.key, as_of),
        as_of=as_of,
        parameters=config.parameters or {},
    )


def build_historical_target_weights(
    db: Session,
    config: StrategyConfig,
    *,
    as_of: date,
) -> dict[str, float]:
    return build_historical_target_portfolio(
        db,
        config,
        as_of=as_of,
    ).target_weights


def _backtest_holding_contexts(
    db: Session,
    *,
    positions: dict[str, int],
    entry_metadata: dict[str, dict[str, Any]],
    histories: dict[str, list[MarketDailyBar]],
    stocks: dict[str, Stock],
    cash: float,
    as_of: date,
) -> list[HoldingContext]:
    visible = {
        symbol: [
            row
            for row in histories.get(symbol, [])
            if row.trade_date <= as_of.isoformat()
        ]
        for symbol in positions
    }
    total_asset = cash + sum(
        quantity * float(visible[symbol][-1].close)
        for symbol, quantity in positions.items()
        if visible.get(symbol)
    )
    if total_asset <= 0:
        return []
    contexts = []
    for symbol, quantity in positions.items():
        rows = visible.get(symbol) or []
        if not rows:
            continue
        metadata = entry_metadata.get(symbol, {})
        entry_date = date.fromisoformat(
            str(metadata.get("entry_date") or rows[0].trade_date)
        )
        latest = rows[-1]
        prior_window = rows[-21:-1] or rows[-20:]
        stock = stocks[symbol]
        cutoff = datetime.combine(as_of, time(16, 30))
        risk_event = db.scalar(
            select(StockEvent.id).where(
                StockEvent.stock_id == stock.id,
                StockEvent.event_type.in_(BLOCKING_EVENTS),
                StockEvent.published_at >= cutoff - timedelta(days=7),
                StockEvent.published_at <= cutoff,
            )
        )
        contexts.append(
            HoldingContext(
                symbol=symbol,
                current_weight=(quantity * float(latest.close) / total_asset),
                entry_date=entry_date,
                held_trading_days=sum(
                    1
                    for row in rows
                    if entry_date < date.fromisoformat(row.trade_date) <= as_of
                ),
                latest_close=float(latest.close),
                low_20d=min(float(row.low) for row in prior_window),
                highest_close=max(
                    float(row.close)
                    for row in rows
                    if row.trade_date >= entry_date.isoformat()
                ),
                entry_atr=float(metadata.get("entry_atr") or 0),
                risk_blocked=bool(
                    stock.status != "active"
                    or "ST" in stock.name.upper()
                    or risk_event is not None
                ),
            )
        )
    return contexts


def _max_drawdown(equity: list[float]) -> float:
    peak = 0.0
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1)
    return worst


def _period_metrics(equity: list[float]) -> tuple[float, float, float]:
    if len(equity) < 2 or equity[0] <= 0:
        return 0.0, 0.0, _max_drawdown(equity)
    returns = [
        equity[index] / equity[index - 1] - 1
        for index in range(1, len(equity))
        if equity[index - 1] > 0
    ]
    years = max((len(equity) - 1) / 252, 1 / 252)
    annualized = (equity[-1] / equity[0]) ** (1 / years) - 1
    volatility = statistics.pstdev(returns) if len(returns) > 1 else 0
    sharpe = (_mean(returns) / volatility * math.sqrt(252)) if volatility > 0 else 0
    return annualized, sharpe, _max_drawdown(equity)


def run_quant_backtest(
    db: Session,
    config: StrategyConfig,
    *,
    start_date: str,
    end_date: str,
    portfolio_builder: Callable[..., dict[str, float]] | None = None,
) -> tuple[BacktestMetrics, StrategyBacktestQualification]:
    definition = db.get(StrategyDefinition, config.strategy_definition_id)
    if definition is None or definition.key not in QUANT_STRATEGY_SPECS:
        raise ValueError("配置不属于独立量化策略")
    if config.mode != "SIMULATION" or not config.simulation_account_id:
        raise ValueError("独立策略回测仅允许模拟盘")
    if start_date >= end_date:
        raise ValueError("回测日期范围无效")
    demo = db.scalar(
        select(MarketDailyBar.id).where(
            MarketDailyBar.trade_date >= start_date,
            MarketDailyBar.trade_date <= end_date,
            func.lower(MarketDailyBar.source).like("%demo%"),
        )
    )
    if demo is not None:
        raise ValueError("回测禁止使用演示数据")

    spec = QUANT_STRATEGY_SPECS[definition.key]
    dates = list(
        db.scalars(
            select(MarketDailyBar.trade_date)
            .join(Stock, Stock.id == MarketDailyBar.stock_id)
            .where(
                MarketDailyBar.trade_date >= start_date,
                MarketDailyBar.trade_date <= end_date,
                MarketDailyBar.quality_status == "valid",
                MarketDailyBar.adjusted_close.is_not(None),
                MarketDailyBar.adjustment_factor.is_not(None),
                MarketDailyBar.adjustment_factor > 0,
                Stock.instrument_type == spec.asset_type,
            )
            .distinct()
            .order_by(MarketDailyBar.trade_date)
        )
    )
    if len(dates) < 2:
        raise ValueError("回测日线不足")
    stocks = {
        stock.symbol: stock
        for stock in db.scalars(
            select(Stock).where(
                Stock.status == "active",
                Stock.instrument_type == spec.asset_type,
            )
        )
    }
    historical_rows = list(
        db.scalars(
            select(MarketDailyBar).where(
                MarketDailyBar.trade_date >= start_date,
                MarketDailyBar.trade_date <= end_date,
                MarketDailyBar.quality_status == "valid",
                MarketDailyBar.adjusted_close.is_not(None),
                MarketDailyBar.adjustment_factor.is_not(None),
                MarketDailyBar.adjustment_factor > 0,
            )
        )
    )
    bars = {(row.stock_id, row.trade_date): row for row in historical_rows}
    symbols_by_id = {stock.id: symbol for symbol, stock in stocks.items()}
    histories: dict[str, list[MarketDailyBar]] = {symbol: [] for symbol in stocks}
    for row in historical_rows:
        if symbol := symbols_by_id.get(row.stock_id):
            histories[symbol].append(row)
    for rows in histories.values():
        rows.sort(key=lambda item: item.trade_date)

    account = db.get(SimulationAccount, config.simulation_account_id)
    initial_cash = float(account.initial_cash if account else 2_000_000)
    cash = initial_cash
    positions: dict[str, int] = {}
    entry_metadata: dict[str, dict[str, Any]] = {}
    consumed_reports: set[tuple[str, str]] = set()
    previous_targets: dict[str, float] = {}
    trade_count = 0
    expected_quotes = 0
    available_quotes = 0
    curve: list[dict[str, object]] = []
    commission_rate = float(account.commission_rate if account else 0.0003)
    minimum_commission = float(account.min_commission if account else 5)
    stamp_tax_rate = float(account.stamp_tax_rate if account else 0.0005)
    slippage_bps = float(account.slippage_bps if account else 5)
    slippage = slippage_bps / 10_000

    for index in range(len(dates) - 1):
        signal_date = date.fromisoformat(dates[index])
        execution_date = dates[index + 1]
        rebalance_applied = _schedule_matches(
            spec.rebalance_frequency,
            dates,
            index,
        )
        if rebalance_applied:
            if portfolio_builder is not None:
                built = portfolio_builder(db, config, as_of=signal_date)
                targets = (
                    dict(built.target_weights)
                    if isinstance(built, TargetPortfolio)
                    else dict(built)
                )
                target_features = (
                    dict(built.features)
                    if isinstance(built, TargetPortfolio)
                    else {}
                )
            else:
                raw = build_historical_target_portfolio(
                    db,
                    config,
                    as_of=signal_date,
                )
                adjusted = apply_holding_policy(
                    raw,
                    holdings=_backtest_holding_contexts(
                        db,
                        positions=positions,
                        entry_metadata=entry_metadata,
                        histories=histories,
                        stocks=stocks,
                        cash=cash,
                        as_of=signal_date,
                    ),
                    consumed_reports=consumed_reports,
                    parameters=config.parameters or {},
                )
                targets = dict(adjusted.target_weights)
                target_features = dict(adjusted.features)
        else:
            targets = previous_targets
            target_features = {}
        symbols = sorted(set(targets) | set(positions))
        opening_prices: dict[str, float] = {}
        for symbol in symbols:
            stock = stocks.get(symbol)
            expected_quotes += 1
            row = bars.get((stock.id, execution_date)) if stock else None
            if row and row.open > 0:
                opening_prices[symbol] = float(row.open)
                available_quotes += 1
        valuation_prices = dict(opening_prices)
        carried_symbols: set[str] = set()
        for symbol in positions:
            if symbol in valuation_prices:
                continue
            previous_rows = [
                row
                for row in histories.get(symbol, [])
                if row.trade_date < execution_date and row.close > 0
            ]
            if previous_rows:
                valuation_prices[symbol] = float(previous_rows[-1].close)
                carried_symbols.add(symbol)
        current_equity = cash + sum(
            quantity * valuation_prices.get(symbol, 0)
            for symbol, quantity in positions.items()
        )
        quotes_complete = len(opening_prices) == len(symbols)
        rebalance_executed = rebalance_applied and quotes_complete
        if rebalance_executed and symbols:
            target_quantities = {
                symbol: math.floor(
                    current_equity
                    * weight
                    / (opening_prices[symbol] * (1 + slippage))
                    / max(stocks[symbol].lot_size, 1)
                )
                * max(stocks[symbol].lot_size, 1)
                for symbol, weight in targets.items()
            }
            for symbol in symbols:
                current_quantity = positions.get(symbol, 0)
                target_quantity = target_quantities.get(symbol, 0)
                difference = target_quantity - current_quantity
                if difference >= 0:
                    continue
                quantity = -difference
                price = opening_prices[symbol] * (1 - slippage)
                notional = price * quantity
                commission = max(notional * commission_rate, minimum_commission)
                proceeds = notional - commission - notional * stamp_tax_rate
                cash += proceeds
                positions[symbol] = target_quantity
                if target_quantity == 0:
                    entry_metadata.pop(symbol, None)
                trade_count += 1
            for symbol in symbols:
                current_quantity = positions.get(symbol, 0)
                target_quantity = target_quantities.get(symbol, 0)
                difference = target_quantity - current_quantity
                if difference <= 0:
                    continue
                price = opening_prices[symbol] * (1 + slippage)
                notional = price * difference
                commission = max(notional * commission_rate, minimum_commission)
                total_cost = notional + commission
                if total_cost > cash:
                    continue
                cash -= total_cost
                positions[symbol] = target_quantity
                if current_quantity == 0:
                    features = target_features.get(symbol, {})
                    metadata = {
                        "entry_date": execution_date,
                        "entry_atr": features.get("atr_20d"),
                        "report_period": features.get("report_period"),
                    }
                    entry_metadata[symbol] = metadata
                    if metadata["report_period"]:
                        consumed_reports.add(
                            (symbol, str(metadata["report_period"]))
                        )
                trade_count += 1
            positions = {symbol: quantity for symbol, quantity in positions.items() if quantity > 0}
            previous_targets = dict(targets)
        equity = cash + sum(
            quantity * valuation_prices.get(symbol, 0)
            for symbol, quantity in positions.items()
        )
        curve.append(
            {
                "trade_date": execution_date,
                "equity": equity,
                "precision": (
                    "carried_last_close"
                    if carried_symbols
                    else "next_day_open"
                ),
                "rebalance_applied": rebalance_executed,
            }
        )

    equity_values = [float(row["equity"]) for row in curve]
    final_equity = equity_values[-1] if equity_values else cash
    split_index = max(1, int(len(equity_values) * 0.70))
    out_of_sample_equity = equity_values[split_index - 1 :]
    annualized, sharpe, out_of_sample_drawdown = _period_metrics(
        out_of_sample_equity
    )
    completeness = available_quotes / expected_quotes if expected_quotes else 0
    metrics = BacktestMetrics(
        trading_days=len(curve),
        data_completeness=completeness,
        annualized_return=annualized,
        sharpe_ratio=sharpe,
        max_drawdown=out_of_sample_drawdown,
        trade_count=trade_count,
        final_equity=final_equity,
        equity_curve=tuple(curve),
    )
    fingerprint = configuration_fingerprint(
        config.parameters or {},
        simulation_account_id=config.simulation_account_id,
        strategy_version=definition.version,
    )
    backtest_run = BacktestRun(
        strategy_definition_id=definition.id,
        strategy_version=definition.version,
        parameters=config.parameters or {},
        universe={"strategy_config_id": config.id, "point_in_time": True},
        benchmark_symbol=str(
            (config.parameters or {}).get("benchmark_symbol", "000300.SH")
        ),
        timeframe="1d",
        start_date=start_date,
        end_date=end_date,
        adjustment_mode="point_in_time_factor",
        data_provider="point_in_time_real",
        initial_cash=initial_cash,
        cost_settings={
            "commission_rate": commission_rate,
            "minimum_commission": minimum_commission,
            "stamp_tax_rate": stamp_tax_rate,
            "slippage_bps": slippage_bps,
        },
        status="completed",
        metrics={
            "trading_days": metrics.trading_days,
            "data_completeness": metrics.data_completeness,
            "out_of_sample_start_date": (
                curve[split_index]["trade_date"]
                if split_index < len(curve)
                else curve[-1]["trade_date"]
            ),
            "annualized_return": metrics.annualized_return,
            "sharpe_ratio": metrics.sharpe_ratio,
            "max_drawdown": metrics.max_drawdown,
            "trade_count": metrics.trade_count,
            "final_equity": metrics.final_equity,
            "precision": "next_day_open",
            "equity_curve": list(metrics.equity_curve),
        },
    )
    db.add(backtest_run)
    db.flush()
    qualification = StrategyBacktestQualification(
        strategy_config_id=config.id,
        backtest_run_id=backtest_run.id,
        config_fingerprint=fingerprint,
        strategy_version=definition.version,
        data_version=str((config.parameters or {}).get("data_version", "1")),
        trading_days=metrics.trading_days,
        data_completeness=metrics.data_completeness,
        out_of_sample_annualized_return=metrics.annualized_return,
        sharpe_ratio=metrics.sharpe_ratio,
        max_drawdown=metrics.max_drawdown,
        trade_count=metrics.trade_count,
        qualified=qualification_passes(metrics),
    )
    db.add(qualification)
    db.commit()
    db.refresh(qualification)
    return metrics, qualification


def _mean(values):
    rows = list(values)
    return sum(rows) / len(rows) if rows else 0.0
