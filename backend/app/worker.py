from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import logging
import time
from datetime import date, datetime, time as wall_time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from .config import get_settings
from .database import SessionLocal
from .data_sync import (
    mark_provider_failure,
    mark_provider_success,
    refresh_quotes,
    sync_corporate_events,
    sync_stock_master_sources,
)
from .models import (
    DataSourceState,
    FinancialReportSnapshot,
    NotificationChannel,
    NotificationDelivery,
    MarketDailyBar,
    MarketDailyMetric,
    Position,
    QuantPortfolioDecision,
    QuantStrategyTask,
    Stock,
    StrategyConfig,
    StrategyDefinition,
    StrategySchedule,
    WatchlistItem,
    now,
)
from .notifications import deliver_channel
from .providers import corporate_event_router, market_router, trading_calendar_service
from .probability_portfolio.config import PROBABILITY_PORTFOLIO_DEFAULTS
from .probability_portfolio.market_snapshot import sync_probability_market_data
from .probability_portfolio.candidates import build_scored_candidates
from .probability_portfolio.observation import (
    finalize_probability_training_samples,
    pending_observation_symbols,
    record_probability_observation,
)
from .probability_portfolio.training import train_and_store_probability_model
from .runtime_bootstrap import wait_for_runtime_database
from .trading_agents.market_snapshot import sync_agent_market_data
from .quant_strategies.data_sync import (
    configured_benchmark_symbols,
    configured_etf_symbols,
    quant_sync_stock_universe,
    sync_adjustment_rows,
    sync_daily_rows,
    sync_etf_master_rows,
    sync_financial_rows,
    sync_metric_rows,
)
from .quant_strategies.performance import record_quant_daily_performance

LOGGER = logging.getLogger("gupiao.worker")
SHANGHAI = ZoneInfo("Asia/Shanghai")
AGENT_SNAPSHOT_RETRY_SECONDS = 120
PROBABILITY_SNAPSHOT_RETRY_SECONDS = 15


def quote_poll_scope(current: datetime | None = None) -> str | None:
    current = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return None
    current_time = current.time().replace(tzinfo=None)
    if (
        wall_time(9, 30) <= current_time <= wall_time(10, 0)
        or wall_time(10, 30) <= current_time <= wall_time(10, 45)
    ):
        return "exit"
    if wall_time(14, 35) <= current_time <= wall_time(14, 41):
        return "entry"
    return None


def event_poll_scope(current: datetime | None = None) -> bool:
    current = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return False
    current_time = current.time().replace(tzinfo=None)
    return (
        wall_time(9, 30) <= current_time < wall_time(10, 0)
        or wall_time(14, 20) <= current_time < wall_time(14, 35)
        or wall_time(16, 10) <= current_time < wall_time(16, 30)
    )


def agent_snapshot_scope(current: datetime | None = None) -> bool:
    current = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return False
    current_time = current.time().replace(tzinfo=None)
    return wall_time(13, 25) <= current_time < wall_time(13, 30)


def quant_data_sync_scope(current: datetime | None = None) -> bool:
    current = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return False
    current_time = current.time().replace(tzinfo=None)
    return wall_time(16, 15) <= current_time < wall_time(23, 0)


@dataclass(frozen=True)
class QuantDataSyncPoll:
    future: Future | None
    last_sync_date: date | None
    last_attempt_seconds: float | None
    result: dict[str, object] | None
    started: bool


def poll_due_quant_data_sync(
    *,
    current: datetime,
    current_seconds: float,
    future: Future | None,
    last_sync_date: date | None,
    last_attempt_seconds: float | None,
    submit,
) -> QuantDataSyncPoll:
    if future is not None:
        if not future.done():
            return QuantDataSyncPoll(
                future,
                last_sync_date,
                last_attempt_seconds,
                None,
                False,
            )
        try:
            result = future.result()
        except Exception as exc:
            result = {"stocks": 0, "errors": 1, "message": str(exc)[:200]}
        if not result.get("errors") or result.get("attempt_completed"):
            last_sync_date = current.date()
        return QuantDataSyncPoll(
            None,
            last_sync_date,
            last_attempt_seconds,
            result,
            False,
        )
    retry_due = (
        last_attempt_seconds is None
        or current_seconds - last_attempt_seconds >= 60
    )
    if (
        quant_data_sync_scope(current)
        and last_sync_date != current.date()
        and retry_due
    ):
        return QuantDataSyncPoll(
            submit(current),
            last_sync_date,
            current_seconds,
            None,
            True,
        )
    return QuantDataSyncPoll(
        None,
        last_sync_date,
        last_attempt_seconds,
        None,
        False,
    )


def poll_quant_market_data(
    *,
    provider=None,
    router=None,
    session_factory=SessionLocal,
    current: datetime | None = None,
    trading_days: set[str] | None = None,
) -> dict[str, int]:
    current = current or datetime.now(SHANGHAI)
    use_default_providers = provider is None and router is None
    if trading_days is None and use_default_providers:
        try:
            trading_days = set(
                trading_calendar_service().trading_days(
                    start=current.date() - timedelta(days=3650),
                    end=current.date() + timedelta(days=30),
                )
            )
            if not trading_days:
                raise RuntimeError("交易所日历返回空结果")
        except Exception as exc:
            return {
                "stocks": 0,
                "etfs": 0,
                "daily_rows": 0,
                "errors": 1,
                "message": f"交易所日历不可用: {exc}"[:200],
            }
    with session_factory() as db:
        route = router or (None if provider is not None else market_router())
        source = provider
        if source is None:
            for candidate in route.providers:
                if (
                    candidate.name == "tushare"
                    and "adjustment" in candidate.capabilities
                    and candidate.health()[0]
                ):
                    source = candidate
                    break
            selection_errors = []
            for capability in ("adjustment", "etf_daily", "daily") if source is None else ():
                try:
                    source = route.select(capability)
                    break
                except Exception as exc:
                    selection_errors.append(str(exc))
            if source is None:
                return {
                    "stocks": 0,
                    "etfs": 0,
                    "errors": 1,
                    "message": "; ".join(selection_errors)[:200],
                }
            route.providers = [
                source,
                *(item for item in route.providers if item is not source),
            ]

        dataset_states = {
            provider: db.scalar(
                select(DataSourceState).where(DataSourceState.provider == provider)
            )
            for provider in (
                "quant_stock_daily",
                "quant_daily_metric",
                "quant_financial",
                "quant_etf_daily",
                "quant_benchmark_daily",
            )
        }
        for provider_name, state in dataset_states.items():
            if state is None:
                state = DataSourceState(
                    provider=provider_name,
                    enabled=True,
                    capabilities=[provider_name],
                    stale_after_seconds=129600,
                )
                db.add(state)
                dataset_states[provider_name] = state
            state.healthy = False
            state.last_checked_at = current
            state.last_error = "同步进行中"
        db.commit()

        def routed_rows(capability: str, method: str, **kwargs):
            if provider is not None:
                return source.name, getattr(source, method)(**kwargs)
            routed = route.call(capability, method, **kwargs)
            return routed.provider, list(routed.data)

        etf_symbols = set(configured_etf_symbols(db))
        try:
            _etf_source, etf_master = routed_rows("etf_master", "etf_master")
            etf_count = sync_etf_master_rows(
                db,
                [
                    row
                    for row in etf_master
                    if str(row.get("ts_code") or row.get("symbol") or row.get("代码") or "").upper()
                    in etf_symbols
                ],
            )
        except Exception:
            db.rollback()
            etf_count = 0
        stocks = quant_sync_stock_universe(db, limit=800)
        updated = 0
        daily_rows = 0
        stock_daily_errors = 0
        metric_errors = 0
        financial_errors = 0
        benchmark_errors = 0
        etf_errors = 0
        end = current.date().isoformat()
        batch_daily: dict[str, dict] | None = None
        batch_adjustments: dict[str, dict] | None = None
        batch_metrics: dict[str, dict] | None = None
        batch_financial: dict[str, list[dict]] | None = None
        batch_financial_error: str | None = None
        if all(
            callable(getattr(source, method, None))
            for method in (
                "daily_cross_section",
                "adjustment_cross_section",
                "daily_metric_cross_section",
            )
        ):
            try:
                batch_daily = {
                    str(row.get("symbol") or "").upper(): row
                    for row in source.daily_cross_section(end)
                    if row.get("symbol")
                }
            except Exception:
                batch_daily = None
            try:
                batch_adjustments = {
                    str(row.get("symbol") or "").upper(): row
                    for row in source.adjustment_cross_section(end)
                    if row.get("symbol")
                }
            except Exception:
                batch_adjustments = None
            try:
                batch_metrics = {
                    str(row.get("symbol") or "").upper(): row
                    for row in source.daily_metric_cross_section(end)
                    if row.get("symbol")
                }
            except Exception:
                batch_metrics = None
        financial_cross_section = getattr(
            source,
            "financial_report_cross_sections",
            None,
        )
        if callable(financial_cross_section):
            financial_baseline_exists = db.scalar(
                select(FinancialReportSnapshot.id).limit(1)
            ) is not None
            period_count = 4 if financial_baseline_exists else 12
            quarter = ((current.month - 1) // 3) * 3
            quarter_year = current.year
            if quarter == 0:
                quarter = 12
                quarter_year -= 1
            periods = []
            for _index in range(period_count):
                periods.append(
                    date(
                        quarter_year,
                        quarter,
                        31 if quarter in {3, 12} else 30,
                    ).isoformat()
                )
                quarter -= 3
                if quarter <= 0:
                    quarter = 12
                    quarter_year -= 1
            try:
                batch_financial = {}
                for row in financial_cross_section(periods):
                    symbol = str(row.get("symbol") or "").upper()
                    if symbol:
                        batch_financial.setdefault(symbol, []).append(row)
            except Exception as exc:
                batch_financial = {}
                batch_financial_error = str(exc)[:1000]
        for stock in stocks:
            existing_count, latest_date = db.execute(
                select(
                    func.count(MarketDailyBar.id),
                    func.max(MarketDailyBar.trade_date),
                ).where(MarketDailyBar.stock_id == stock.id)
            ).one()
            start_date = current.date() - timedelta(days=1200)
            if int(existing_count or 0) >= 520 and latest_date:
                start_date = max(
                    current.date() - timedelta(days=30),
                    date.fromisoformat(str(latest_date)) - timedelta(days=5),
                )
            start = start_date.isoformat()
            daily_ok = False
            metric_ok = False
            financial_ok = False
            try:
                if int(existing_count or 0) >= 520 and batch_daily is not None:
                    daily_source = source.name
                    bars = [batch_daily[stock.symbol]] if stock.symbol in batch_daily else []
                else:
                    daily_source, bars = routed_rows(
                        "daily",
                        "bars",
                        symbol=stock.symbol,
                        timeframe="1d",
                        start=start,
                        end=end,
                    )
                daily_rows += sync_daily_rows(
                    db,
                    stock,
                    bars,
                    source=daily_source,
                    amount_multiplier=(
                        1
                        if batch_daily is not None and int(existing_count or 0) >= 520
                        else 1000 if daily_source == "tushare" else 1
                    ),
                    volume_multiplier=(
                        1
                        if batch_daily is not None and int(existing_count or 0) >= 520
                        else 100 if daily_source == "tushare" else 1
                    ),
                )
                if int(existing_count or 0) >= 520 and batch_adjustments is not None:
                    adjusted = (
                        [batch_adjustments[stock.symbol]]
                        if stock.symbol in batch_adjustments
                        else []
                    )
                else:
                    adjusted = source.adjustment_factors(
                        stock.symbol,
                        start=start,
                        end=end,
                    )
                sync_adjustment_rows(db, stock, adjusted, source=source.name)
                daily_ok = db.scalar(
                    select(MarketDailyBar.id).where(
                        MarketDailyBar.stock_id == stock.id,
                        MarketDailyBar.trade_date == end,
                        MarketDailyBar.adjusted_close.is_not(None),
                        MarketDailyBar.adjustment_factor.is_not(None),
                        MarketDailyBar.adjustment_factor > 0,
                    )
                ) is not None
                if not daily_ok:
                    raise RuntimeError("当日原始日线或复权因子缺失")
            except Exception:
                db.rollback()
                stock_daily_errors += 1
            try:
                if int(existing_count or 0) >= 520 and batch_metrics is not None:
                    metrics = (
                        [batch_metrics[stock.symbol]]
                        if stock.symbol in batch_metrics
                        else []
                    )
                else:
                    metrics = source.daily_metrics(
                        stock.symbol,
                        start=start,
                        end=end,
                    )
                sync_metric_rows(db, stock, metrics, source=source.name)
                metric_ok = db.scalar(
                    select(MarketDailyMetric.id).where(
                        MarketDailyMetric.stock_id == stock.id,
                        MarketDailyMetric.trade_date == end,
                    )
                ) is not None
                if not metric_ok:
                    raise RuntimeError("当日估值指标缺失")
            except Exception:
                db.rollback()
                metric_errors += 1
            try:
                if batch_financial is not None:
                    if batch_financial_error:
                        raise RuntimeError(batch_financial_error)
                    financial = batch_financial.get(stock.symbol, [])
                else:
                    financial = source.financial_reports(stock.symbol)
                sync_financial_rows(
                    db,
                    stock,
                    financial,
                    source=source.name,
                    trading_days=trading_days,
                )
                financial_ok = db.scalar(
                    select(FinancialReportSnapshot.id).where(
                        FinancialReportSnapshot.stock_id == stock.id,
                        FinancialReportSnapshot.available_on <= end,
                    )
                ) is not None
                if not financial_ok:
                    raise RuntimeError("尚无可用点时财务报告")
            except Exception as exc:
                db.rollback()
                financial_errors += 1
                dataset_states["quant_financial"].last_error = str(exc)[:1000]
                db.commit()
            if daily_ok and metric_ok and financial_ok:
                updated += 1

        benchmark_symbols = set(configured_benchmark_symbols(db)) - etf_symbols
        for symbol in sorted(benchmark_symbols):
            benchmark = db.scalar(select(Stock).where(Stock.symbol == symbol))
            if benchmark is None:
                code, suffix = symbol.split(".", 1)
                benchmark = Stock(
                    code=code,
                    exchange="SSE" if suffix == "SH" else "SZSE",
                    symbol=symbol,
                    name=f"策略基准 {code}",
                    status="benchmark",
                    instrument_type="INDEX",
                    lot_size=1,
                    settlement_days=0,
                )
                db.add(benchmark)
                db.commit()
            try:
                daily_source, bars = routed_rows(
                    "daily",
                    "bars",
                    symbol=symbol,
                    timeframe="1d",
                    start=(current.date() - timedelta(days=1200)).isoformat(),
                    end=end,
                )
                daily_rows += sync_daily_rows(
                    db,
                    benchmark,
                    bars,
                    source=daily_source,
                    amount_multiplier=1000 if daily_source == "tushare" else 1,
                    volume_multiplier=100 if daily_source == "tushare" else 1,
                )
                benchmark_ok = db.scalar(
                    select(MarketDailyBar.id).where(
                        MarketDailyBar.stock_id == benchmark.id,
                        MarketDailyBar.trade_date == end,
                        MarketDailyBar.quality_status == "valid",
                        MarketDailyBar.close > 0,
                        ~func.lower(MarketDailyBar.source).like("%demo%"),
                    )
                ) is not None
                if not benchmark_ok:
                    raise RuntimeError("当日基准指数日线缺失")
            except Exception:
                db.rollback()
                benchmark_errors += 1
        etfs = list(
            db.scalars(
                select(Stock)
                .where(
                    Stock.status == "active",
                    Stock.instrument_type == "ETF",
                    Stock.symbol.in_(etf_symbols),
                )
                .order_by(Stock.symbol)
            )
        )
        etfs_updated = 0
        for stock in etfs:
            try:
                daily_source, bars = routed_rows(
                    "etf_daily",
                    "etf_bars",
                    symbol=stock.symbol,
                    start=(current.date() - timedelta(days=1200)).isoformat(),
                    end=end,
                )
                daily_rows += sync_daily_rows(
                    db,
                    stock,
                    bars,
                    source=daily_source,
                )
                etf_ok = db.scalar(
                    select(MarketDailyBar.id).where(
                        MarketDailyBar.stock_id == stock.id,
                        MarketDailyBar.trade_date == end,
                        MarketDailyBar.quality_status == "valid",
                        MarketDailyBar.adjusted_close.is_not(None),
                        MarketDailyBar.adjustment_factor.is_not(None),
                        MarketDailyBar.adjustment_factor > 0,
                        ~func.lower(MarketDailyBar.source).like("%demo%"),
                    )
                ) is not None
                if not etf_ok:
                    raise RuntimeError("当日 ETF 日线缺失")
                etfs_updated += 1
            except Exception:
                db.rollback()
                etf_errors += 1
        stock_count = len(stocks)
        minimum_stock_success = max(1, int(stock_count * 0.98 + 0.999999))

        def stock_dataset_healthy(error_count: int) -> bool:
            return (
                stock_count > 0
                and stock_count - error_count >= minimum_stock_success
            )

        state_results = {
            "quant_stock_daily": (
                stock_dataset_healthy(stock_daily_errors),
                stock_daily_errors,
            ),
            "quant_daily_metric": (
                stock_dataset_healthy(metric_errors),
                metric_errors,
            ),
            "quant_financial": (
                stock_dataset_healthy(financial_errors),
                financial_errors,
            ),
            "quant_etf_daily": (bool(etfs) and etf_errors == 0, etf_errors),
            "quant_benchmark_daily": (
                bool(benchmark_symbols) and benchmark_errors == 0,
                benchmark_errors,
            ),
        }
        for provider_name, (healthy, error_count) in state_results.items():
            state = dataset_states[provider_name]
            state.healthy = healthy
            state.last_checked_at = current
            if healthy:
                state.last_error = None
            elif state.last_error == "同步进行中":
                state.last_error = (
                    f"同步失败 {error_count} 项"
                    if error_count
                    else "没有可同步证券"
                )
        db.commit()
        for provider_name, (healthy, _error_count) in state_results.items():
            if healthy:
                continue
            channels = list(
                db.scalars(
                    select(NotificationChannel).where(
                        NotificationChannel.enabled.is_(True)
                    )
                )
            )
            for channel in channels:
                if "quant_strategy_data_failed" not in (channel.event_types or []):
                    continue
                duplicate = any(
                    delivery.payload.get("dataset") == provider_name
                    and delivery.payload.get("trading_date") == end
                    for delivery in db.scalars(
                        select(NotificationDelivery).where(
                            NotificationDelivery.channel_id == channel.id,
                            NotificationDelivery.event_type
                            == "quant_strategy_data_failed",
                        )
                    )
                )
                if duplicate:
                    continue
                db.add(
                    NotificationDelivery(
                        channel_id=channel.id,
                        event_type="quant_strategy_data_failed",
                        severity="error",
                        subject="独立量化数据批次失败",
                        payload={
                            "dataset": provider_name,
                            "trading_date": end,
                            "error": dataset_states[provider_name].last_error,
                        },
                        status="pending",
                    )
                )
        db.commit()
        errors = (
            stock_daily_errors
            + metric_errors
            + financial_errors
            + benchmark_errors
            + etf_errors
        )
        performance = {"recorded": 0, "skipped": 0, "paused": 0}
        if state_results["quant_stock_daily"][0] or state_results["quant_etf_daily"][0]:
            performance = record_quant_daily_performance(db, current=current)
        return {
            "stocks": updated,
            "etfs": etfs_updated if etf_count or etfs else 0,
            "daily_rows": daily_rows,
            "errors": errors,
            "attempt_completed": True,
            "performance_recorded": performance["recorded"],
            "performance_paused": performance["paused"],
        }


def probability_snapshot_scope(current: datetime | None = None) -> bool:
    current = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return False
    current_time = current.time().replace(tzinfo=None)
    return wall_time(14, 40) <= current_time < wall_time(14, 40, 30)


def probability_preheat_scope(current: datetime | None = None) -> bool:
    current = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return False
    current_time = current.time().replace(tzinfo=None)
    return wall_time(13, 40) <= current_time < wall_time(14, 10)


def probability_observation_scope(current: datetime | None = None) -> bool:
    current = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return False
    current_time = current.time().replace(tzinfo=None)
    return wall_time(14, 40) <= current_time < wall_time(14, 41)


def probability_label_scope(current: datetime | None = None) -> bool:
    current = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return False
    current_time = current.time().replace(tzinfo=None)
    return wall_time(10, 30) <= current_time <= wall_time(10, 45)


def _probability_config(db):
    return db.scalar(
        select(StrategyConfig)
        .join(StrategyDefinition)
        .where(
            StrategyDefinition.key == "overnight_probability_portfolio",
            StrategyConfig.enabled.is_(True),
            StrategyConfig.mode == "SIMULATION",
        )
        .limit(1)
    )


def poll_probability_market_snapshot(
    *,
    router=None,
    session_factory=SessionLocal,
    current: datetime | None = None,
    completed_at: datetime | None = None,
    record_observation: bool = True,
) -> dict[str, int]:
    current = current or datetime.now(SHANGHAI)
    with session_factory() as db:
        config = _probability_config(db)
        if config is None:
            return {"skipped": 1}
        try:
            result = sync_probability_market_data(
                db,
                config,
                router or market_router(),
                current=current,
            )
            completed_at = completed_at or datetime.now(SHANGHAI)
            observation_window = current.replace(
                hour=14,
                minute=40,
                second=0,
                microsecond=0,
            )
            observation_deadline = observation_window + timedelta(minutes=1)
            if (
                record_observation
                and not result.get("errors")
                and completed_at.date() == current.date()
                and observation_window <= completed_at < observation_deadline
            ):
                candidates = build_scored_candidates(
                    db,
                    config,
                    current=completed_at,
                )
                observation = record_probability_observation(
                    db,
                    config,
                    current=completed_at,
                    scored_candidates=candidates.scored,
                    rejected_candidates=candidates.rejected,
                    candidate_reasons=candidates.reasons,
                )
                result = {**result, "observation_run_id": observation.id}
            return result
        except Exception as exc:
            db.rollback()
            LOGGER.exception("概率组合行情固化失败")
            return {"skipped": 0, "errors": 1, "message": str(exc)[:200]}


def poll_probability_observation(
    *,
    session_factory=SessionLocal,
    current: datetime | None = None,
) -> dict[str, int]:
    current = current or datetime.now(SHANGHAI)
    with session_factory() as db:
        config = _probability_config(db)
        if config is None:
            return {"skipped": 1}
        candidates = build_scored_candidates(db, config, current=current)
        run = record_probability_observation(
            db,
            config,
            current=current,
            scored_candidates=candidates.scored,
            rejected_candidates=candidates.rejected,
            candidate_reasons=candidates.reasons,
        )
        return {
            "accepted": int((run.summary or {}).get("accepted", 0)),
        }


def poll_due_probability_observation(
    *,
    last_observation_date,
    current: datetime | None = None,
):
    current = current or datetime.now(SHANGHAI)
    if (
        not probability_observation_scope(current)
        or last_observation_date == current.date()
    ):
        return last_observation_date, None
    return current.date(), poll_probability_observation(current=current)


def poll_probability_training_labels(
    *,
    provider=None,
    session_factory=SessionLocal,
    current: datetime | None = None,
) -> dict[str, int]:
    current = current or datetime.now(SHANGHAI)
    with session_factory() as db:
        config = _probability_config(db)
        if config is None:
            return {"skipped": 1}
        calendar = trading_calendar_service()
        try:
            symbols = pending_observation_symbols(
                db,
                config,
                current=current,
                calendar=calendar,
            )
        except Exception:
            db.rollback()
            return {
                "created": 0,
                "skipped": 0,
                "errors": 1,
                "quote_updated": 0,
            }
        if not symbols:
            return {
                "created": 0,
                "skipped": 0,
                "errors": 0,
                "quote_updated": 0,
            }
        try:
            quote_result = _refresh_symbol_quotes(db, symbols, provider)
        except Exception:
            db.rollback()
            return {
                "created": 0,
                "skipped": 0,
                "errors": 1,
                "quote_updated": 0,
            }
        result = finalize_probability_training_samples(
            db,
            config,
            current=current,
            calendar=calendar,
        )
        output = {**result, "quote_updated": quote_result.updated}
        if result["created"] or result["skipped"]:
            parameters = {
                **PROBABILITY_PORTFOLIO_DEFAULTS,
                **(config.parameters or {}),
            }
            artifact = train_and_store_probability_model(
                db,
                through=current.date(),
                feature_version=str(parameters["feature_version"]),
                min_training_samples=int(parameters["min_training_samples"]),
                min_calibration_samples=int(parameters["min_calibration_samples"]),
                max_brier_score=float(parameters["max_brier_score"]),
            )
            output.update(
                {
                    "model_artifact_id": artifact.id,
                    "model_status": artifact.status,
                    "training_sample_count": artifact.training_sample_count,
                    "calibration_sample_count": artifact.calibration_sample_count,
                }
            )
        return output


def agent_snapshot_retry_due(
    last_attempt_seconds: float | None,
    *,
    current_seconds: float,
) -> bool:
    return (
        last_attempt_seconds is None
        or current_seconds - last_attempt_seconds >= AGENT_SNAPSHOT_RETRY_SECONDS
    )


def poll_agent_market_snapshot(
    *,
    router=None,
    session_factory=SessionLocal,
    current: datetime | None = None,
) -> dict[str, int]:
    current = current or datetime.now(SHANGHAI)
    if router is None:
        event_result = poll_corporate_events(session_factory=session_factory)
        if event_result["errors"]:
            return {"skipped": 0, "errors": 1, "message": "公司公告同步失败"}
    with session_factory() as db:
        config = db.scalar(
            select(StrategyConfig)
            .join(StrategyDefinition)
            .where(
                StrategyDefinition.key == "trading_agents_auto",
                StrategyConfig.enabled.is_(True),
                StrategyConfig.mode == "SIMULATION",
            )
            .limit(1)
        )
        if not config:
            return {"skipped": 1}
        parameters = config.parameters or {}
        analysis_schedule_enabled = bool(
            db.scalar(
                select(StrategySchedule.id).where(
                    StrategySchedule.strategy_config_id == config.id,
                    StrategySchedule.trigger_type == "agent_analysis",
                    StrategySchedule.enabled.is_(True),
                )
            )
        )
        if not parameters.get("dry_run", True) and not analysis_schedule_enabled:
            return {"skipped": 1}
        try:
            return sync_agent_market_data(
                db,
                config,
                router or market_router(),
                current=current,
            )
        except Exception as exc:
            db.rollback()
            LOGGER.exception("TradingAgents 行情固化失败")
            return {"skipped": 0, "errors": 1, "message": str(exc)[:200]}


def should_poll_events(
    current: datetime,
    *,
    seconds_since_attempt: float,
    retry_seconds: int,
) -> bool:
    return (
        event_poll_scope(current)
        and seconds_since_attempt >= retry_seconds
    )


def notification_poll_allowed(current: datetime | None = None) -> bool:
    current = current or datetime.now(SHANGHAI)
    return quote_poll_scope(current) is None and not event_poll_scope(current)


def _refresh_symbol_quotes(db, symbols: list[str], provider=None):
    if provider is not None:
        selected_provider = provider
        rows = provider.quotes(symbols)
    else:
        routed = market_router().call("realtime", "quotes", symbols=symbols)
        selected_provider = type("SelectedProvider", (), {"name": routed.provider})()
        rows = list(routed.data)
    quote_provider = type(
        "QuoteRows",
        (),
        {"name": selected_provider.name, "quotes": lambda self, _: rows},
    )()
    return refresh_quotes(db, quote_provider, symbols)


def process_pending_notifications(limit: int = 20) -> int:
    processed = 0
    settings = get_settings()
    with SessionLocal() as db:
        pending = list(
            db.scalars(
                select(NotificationDelivery)
                .where(NotificationDelivery.status == "pending")
                .order_by(NotificationDelivery.id)
                .limit(limit)
            )
        )
        for delivery in pending:
            channel = db.get(NotificationChannel, delivery.channel_id)
            if not channel or not channel.enabled:
                delivery.status = "failed"
                delivery.last_error = "通知渠道不存在或未启用"
                processed += 1
                continue
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
            processed += 1
        db.commit()
    return processed


def poll_watchlist_quotes(provider=None, session_factory=SessionLocal) -> dict[str, int]:
    updated = 0
    missing = 0
    errors = 0
    with session_factory() as db:
        symbols = [
            item.symbol
            for item in db.scalars(
                select(Stock).join(WatchlistItem, WatchlistItem.stock_id == Stock.id).order_by(WatchlistItem.id)
            )
        ]
        if not symbols:
            return {"updated": 0, "missing": 0, "errors": 0}
        try:
            result = _refresh_symbol_quotes(db, symbols, provider)
            updated = result.updated
            missing = len(result.missing)
        except Exception as exc:
            db.rollback()
            try:
                mark_provider_failure(db, getattr(provider, "name", "akshare"), exc)
            except Exception:
                db.rollback()
                LOGGER.exception("自选行情失败状态写入失败")
            updated = 0
            missing = len(symbols)
            errors = 1
    return {"updated": updated, "missing": missing, "errors": errors}


def _strategy_symbols(db) -> list[str]:
    return list(
        db.scalars(
            select(Stock.symbol)
            .where(
                Stock.status == "active",
                Stock.exchange.in_(["SSE", "SZSE"]),
            )
            .order_by(Stock.symbol)
        )
    )


def poll_strategy_quotes(provider=None, session_factory=SessionLocal) -> dict[str, int]:
    with session_factory() as db:
        symbols = _strategy_symbols(db)
        if not symbols:
            return {"updated": 0, "missing": 0, "errors": 0}
        try:
            result = _refresh_symbol_quotes(db, symbols, provider)
            return {
                "updated": result.updated,
                "missing": len(result.missing),
                "errors": 0,
            }
        except Exception as exc:
            db.rollback()
            name = getattr(provider, "name", "market_router")
            try:
                mark_provider_failure(db, name, exc)
            except Exception:
                db.rollback()
                LOGGER.exception("全市场行情失败状态写入失败")
            return {"updated": 0, "missing": len(symbols), "errors": 1}


def poll_position_quotes(provider=None, session_factory=SessionLocal) -> dict[str, int]:
    with session_factory() as db:
        symbols = list(
            db.scalars(
                select(Stock.symbol)
                .join(Position, Position.stock_id == Stock.id)
                .where(
                    Position.mode == "SIMULATION",
                    Position.quantity > 0,
                )
                .order_by(Stock.symbol)
            )
        )
        if not symbols:
            return {"updated": 0, "missing": 0, "errors": 0}
        try:
            result = _refresh_symbol_quotes(db, symbols, provider)
            return {
                "updated": result.updated,
                "missing": len(result.missing),
                "errors": 0,
            }
        except Exception as exc:
            db.rollback()
            name = getattr(provider, "name", "market_router")
            try:
                mark_provider_failure(db, name, exc)
            except Exception:
                db.rollback()
                LOGGER.exception("持仓行情失败状态写入失败")
            return {"updated": 0, "missing": len(symbols), "errors": 1}


def poll_quant_execution_quotes(
    provider=None,
    session_factory=SessionLocal,
    current: datetime | None = None,
) -> dict[str, int]:
    current = current or datetime.now(SHANGHAI)
    with session_factory() as db:
        symbols = set(
            db.scalars(
                select(Stock.symbol)
                .join(Position, Position.stock_id == Stock.id)
                .where(
                    Position.mode == "SIMULATION",
                    Position.quantity > 0,
                )
            )
        )
        tasks = list(
            db.scalars(
                select(QuantStrategyTask).where(
                    QuantStrategyTask.task_type == "execute",
                    QuantStrategyTask.status.in_(
                        ["pending", "retry", "processing"]
                    ),
                    QuantStrategyTask.trading_date == current.date().isoformat(),
                )
            )
        )
        for task in tasks:
            decision_id = (task.payload or {}).get("decision_id")
            decision = db.get(QuantPortfolioDecision, decision_id)
            if decision is not None and decision.status == "ready":
                symbols.update(decision.target_weights or {})
        ready_decisions = db.scalars(
            select(QuantPortfolioDecision).where(
                QuantPortfolioDecision.decision_type == "signal",
                QuantPortfolioDecision.status == "ready",
                QuantPortfolioDecision.trading_date < current.date().isoformat(),
            )
        )
        for decision in ready_decisions:
            symbols.update(decision.target_weights or {})
        ordered = sorted(symbols)
        if not ordered:
            return {"updated": 0, "missing": 0, "errors": 0}
        try:
            result = _refresh_symbol_quotes(db, ordered, provider)
            return {
                "updated": result.updated,
                "missing": len(result.missing),
                "errors": 0,
            }
        except Exception as exc:
            db.rollback()
            name = getattr(provider, "name", "market_router")
            try:
                mark_provider_failure(db, name, exc)
            except Exception:
                db.rollback()
                LOGGER.exception("独立量化执行行情失败状态写入失败")
            return {"updated": 0, "missing": len(ordered), "errors": 1}


def poll_corporate_events(provider=None, session_factory=SessionLocal) -> dict[str, int]:
    with session_factory() as db:
        symbols = _strategy_symbols(db)
        if not symbols:
            return {"created": 0, "updated": 0, "errors": 0}
        end = now().date()
        start = end - timedelta(days=7)
        try:
            if provider is not None:
                selected_provider = provider
                rows = provider.events(
                    symbols=symbols,
                    start=start.isoformat(),
                    end=end.isoformat(),
                )
            else:
                routed = corporate_event_router().call(
                    "corporate_events",
                    "events",
                    symbols=symbols,
                    start=start.isoformat(),
                    end=end.isoformat(),
                )
                selected_provider = type(
                    "SelectedProvider",
                    (),
                    {"name": routed.provider},
                )()
                rows = list(routed.data)
            result = sync_corporate_events(db, rows)
            mark_provider_success(db, selected_provider.name)
            return {"created": result.created, "updated": result.updated, "errors": 0}
        except Exception as exc:
            db.rollback()
            name = getattr(provider, "name", "akshare_events")
            try:
                mark_provider_failure(db, name, exc)
            except Exception:
                db.rollback()
                LOGGER.exception("公告失败状态写入失败")
            return {"created": 0, "updated": 0, "errors": 1}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    wait_for_runtime_database()
    with SessionLocal() as db:
        try:
            result = sync_stock_master_sources(db, market_router().providers)
            LOGGER.info(
                "股票主数据同步完成 providers=%s failures=%s",
                result.providers,
                sorted(result.failures),
            )
        except Exception:
            db.rollback()
            LOGGER.exception("股票主数据同步失败")

    quote_result = poll_strategy_quotes()
    LOGGER.info("启动行情探测完成 result=%s", quote_result)
    event_result = poll_corporate_events()
    LOGGER.info("启动公告探测完成 result=%s", event_result)
    last_quote_poll = time.time()
    last_event_attempt = time.time()
    last_agent_snapshot_date = None
    last_agent_snapshot_attempt = None
    last_probability_snapshot_date = None
    last_probability_snapshot_attempt = None
    last_probability_preheat_date = None
    last_probability_preheat_attempt = None
    last_probability_observation_date = None
    last_probability_label_date = None
    last_probability_label_attempt = None
    last_quant_sync_date = None
    last_quant_sync_attempt = None
    quant_sync_future = None
    quant_sync_executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="quant-data-sync",
    )
    while True:
        try:
            current = time.time()
            current_dt = datetime.now(SHANGHAI)
            quant_sync = poll_due_quant_data_sync(
                current=current_dt,
                current_seconds=current,
                future=quant_sync_future,
                last_sync_date=last_quant_sync_date,
                last_attempt_seconds=last_quant_sync_attempt,
                submit=lambda sync_time: quant_sync_executor.submit(
                    poll_quant_market_data,
                    current=sync_time,
                ),
            )
            quant_sync_future = quant_sync.future
            last_quant_sync_date = quant_sync.last_sync_date
            last_quant_sync_attempt = quant_sync.last_attempt_seconds
            if quant_sync.started:
                LOGGER.info("独立量化点时数据同步已异步启动")
            if quant_sync.result is not None:
                LOGGER.info("独立量化点时数据同步 result=%s", quant_sync.result)
            if (
                agent_snapshot_scope(current_dt)
                and last_agent_snapshot_date != current_dt.date()
                and agent_snapshot_retry_due(
                    last_agent_snapshot_attempt,
                    current_seconds=current,
                )
            ):
                result = poll_agent_market_snapshot(current=current_dt)
                last_agent_snapshot_attempt = time.time()
                if not result.get("errors"):
                    last_agent_snapshot_date = current_dt.date()
                LOGGER.info("TradingAgents 行情固化 result=%s", result)
            if (
                probability_preheat_scope(current_dt)
                and last_probability_preheat_date != current_dt.date()
                and (
                    last_probability_preheat_attempt is None
                    or current - last_probability_preheat_attempt
                    >= PROBABILITY_SNAPSHOT_RETRY_SECONDS
                )
            ):
                result = poll_probability_market_snapshot(
                    current=current_dt,
                    record_observation=False,
                )
                last_probability_preheat_attempt = time.time()
                if not result.get("errors"):
                    last_probability_preheat_date = current_dt.date()
                LOGGER.info("概率组合数据预热 result=%s", result)
            if (
                probability_snapshot_scope(current_dt)
                and last_probability_snapshot_date != current_dt.date()
                and (
                    last_probability_snapshot_attempt is None
                    or current - last_probability_snapshot_attempt
                    >= PROBABILITY_SNAPSHOT_RETRY_SECONDS
                )
            ):
                result = poll_probability_market_snapshot(current=current_dt)
                last_probability_snapshot_attempt = time.time()
                if not result.get("errors"):
                    last_probability_snapshot_date = current_dt.date()
                LOGGER.info("概率组合行情固化 result=%s", result)
            observed_date, result = poll_due_probability_observation(
                last_observation_date=last_probability_observation_date,
            )
            if result is not None:
                last_probability_observation_date = observed_date
                LOGGER.info("概率组合无下单观察 result=%s", result)
            if (
                probability_label_scope(current_dt)
                and last_probability_label_date != current_dt.date()
                and (
                    last_probability_label_attempt is None
                    or current - last_probability_label_attempt
                    >= PROBABILITY_SNAPSHOT_RETRY_SECONDS
                )
            ):
                result = poll_probability_training_labels(current=current_dt)
                last_probability_label_attempt = time.time()
                if not result.get("errors"):
                    last_probability_label_date = current_dt.date()
                LOGGER.info("概率组合训练标签 result=%s", result)
            if notification_poll_allowed(current_dt):
                process_pending_notifications()
            scope = quote_poll_scope(current_dt)
            if scope and current - last_quote_poll >= get_settings().realtime_poll_seconds:
                result = (
                    poll_strategy_quotes()
                    if scope == "entry"
                    else poll_quant_execution_quotes(current=current_dt)
                )
                last_quote_poll = time.time()
                LOGGER.info("%s 窗口行情刷新 result=%s", scope, result)
            if should_poll_events(
                current_dt,
                seconds_since_attempt=current - last_event_attempt,
                retry_seconds=get_settings().corporate_event_sync_seconds,
            ):
                result = poll_corporate_events()
                last_event_attempt = time.time()
                LOGGER.info("入场预热窗口公告刷新 result=%s", result)
        except Exception:
            LOGGER.exception("worker 迭代失败，将在下一轮继续")
        time.sleep(2)


if __name__ == "__main__":
    main()
