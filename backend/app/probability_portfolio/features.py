from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

from ..models import MarketDailyBar, Stock


FEATURE_NAMES = (
    "intraday_return",
    "turnover_amount_log",
    "turnover_rate",
    "vwap_distance",
    "tail_30m_return",
    "close_location",
    "ma5_distance",
    "ma20_distance",
    "momentum_5d",
    "momentum_20d",
    "volatility_20d",
    "average_amount_20d_log",
    "benchmark_ma5_distance",
    "relative_strength",
    "market_breadth",
)


@dataclass(frozen=True)
class FeatureVectorResult:
    accepted: bool
    features: dict[str, float]
    reasons: tuple[str, ...]


def _aware(value: datetime | None, current: datetime) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=current.tzinfo)
    return value.astimezone(current.tzinfo)


def _listing_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _completed_bars(
    bars: Iterable[MarketDailyBar], current: datetime
) -> tuple[list[MarketDailyBar], bool]:
    all_rows = sorted(bars, key=lambda item: item.trade_date)
    future = any(item.trade_date >= current.date().isoformat() for item in all_rows)
    return [item for item in all_rows if item.trade_date < current.date().isoformat()], future


def _distance(value: float, baseline: float) -> float:
    return value / baseline - 1 if baseline else 0.0


def _latest_expected_daily_date(current: datetime) -> date:
    candidate = current.date() - date.resolution
    while candidate.weekday() >= 5:
        candidate -= date.resolution
    return candidate


def _daily_cache_is_current(
    bars: list[MarketDailyBar],
    current: datetime,
) -> bool:
    if not bars:
        return False
    if bars[-1].trade_date >= _latest_expected_daily_date(current).isoformat():
        return True
    captured_at = _aware(bars[-1].captured_at, current)
    return bool(
        captured_at
        and captured_at <= current
        and captured_at.date() == current.date()
    )


def build_feature_vector(
    stock: Stock,
    daily_bars: Iterable[MarketDailyBar],
    benchmark_bars: Iterable[MarketDailyBar],
    *,
    current: datetime,
    source_healthy: bool,
    critical_event: bool,
    market_breadth: float,
    max_quote_age_seconds: int = 60,
) -> FeatureVectorResult:
    reasons: list[str] = []
    max_quote_age_seconds = min(60, max(1, int(max_quote_age_seconds)))
    if stock.exchange not in {"SSE", "SZSE"}:
        reasons.append("交易所不在策略范围")
    if stock.status != "active":
        reasons.append("股票当前不可交易")
    if "ST" in str(stock.name or "").upper():
        reasons.append("ST股票已排除")

    listed = _listing_date(stock.listing_date)
    if listed is None:
        reasons.append("缺少真实上市日期")
    elif (current.date() - listed).days < 60:
        reasons.append("上市时间不足60日")

    if stock.turnover_rate is None:
        reasons.append("缺少真实换手率")
    elif stock.turnover_rate < 0.01:
        reasons.append("换手率不足1%")
    if stock.turnover_amount is None:
        reasons.append("缺少真实成交额")
    elif stock.turnover_amount < 100_000_000:
        reasons.append("成交额不足1亿元")
    if stock.vwap is None or stock.vwap <= 0:
        reasons.append("缺少真实日内VWAP")
    if stock.tail_30m_return is None:
        reasons.append("缺少尾盘30分钟收益")
    if any(value is None for value in (stock.open_price, stock.high_price, stock.low_price)):
        reasons.append("缺少当日开高低数据")
    if stock.last_price is None or stock.last_price <= 0:
        reasons.append("最新价格无效")

    quote_at = _aware(stock.quote_updated_at, current)
    factor_at = _aware(stock.factor_updated_at, current)
    if quote_at is None or factor_at is None:
        reasons.append("行情或因子时间缺失")
    elif quote_at > current or factor_at > current:
        reasons.append("行情时间位于未来")
    elif max((current - quote_at).total_seconds(), (current - factor_at).total_seconds()) > max_quote_age_seconds:
        reasons.append("行情已过期")
    if not source_healthy:
        reasons.append("行情来源不健康")
    if critical_event:
        reasons.append("命中重大事件风险")

    if (
        stock.last_price is not None
        and stock.limit_up_price is not None
        and stock.last_price >= stock.limit_up_price - 1e-9
    ) or (
        stock.last_price is not None
        and stock.limit_down_price is not None
        and stock.last_price <= stock.limit_down_price + 1e-9
    ):
        reasons.append("股票处于涨停或跌停价格")

    completed, future = _completed_bars(daily_bars, current)
    if future:
        reasons.append("日线包含未完成或未来数据")
    if len(completed) < 20:
        reasons.append("已完成日线不足20根")
    elif not _daily_cache_is_current(completed, current):
        reasons.append("日线未覆盖最近已完成交易日")

    benchmark, benchmark_future = _completed_bars(benchmark_bars, current)
    if benchmark_future:
        reasons.append("基准日线包含未完成或未来数据")
    if len(benchmark) < 5:
        reasons.append("基准已完成日线不足5根")
    elif not _daily_cache_is_current(benchmark, current):
        reasons.append("基准日线未覆盖最近已完成交易日")
    elif benchmark[-1].close < statistics.fmean(item.close for item in benchmark[-5:]):
        reasons.append("市场基准未通过MA5过滤")

    intraday_return = float(stock.change_pct or 0) / 100
    if not 0.01 <= intraday_return <= 0.05:
        reasons.append("日内涨幅不在1%至5%范围")

    if reasons:
        return FeatureVectorResult(False, {}, tuple(dict.fromkeys(reasons)))

    closes = [float(item.close) for item in completed[-20:]]
    returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]
    ma5 = statistics.fmean(closes[-5:])
    ma20 = statistics.fmean(closes)
    price = float(stock.last_price)
    low = float(stock.low_price)
    high = float(stock.high_price)
    benchmark_closes = [float(item.close) for item in benchmark[-5:]]
    benchmark_return = benchmark_closes[-1] / benchmark_closes[-2] - 1
    features = {
        "intraday_return": intraday_return,
        "turnover_amount_log": math.log1p(float(stock.turnover_amount)),
        "turnover_rate": float(stock.turnover_rate),
        "vwap_distance": _distance(price, float(stock.vwap)),
        "tail_30m_return": float(stock.tail_30m_return),
        "close_location": (price - low) / (high - low) if high > low else 0.5,
        "ma5_distance": _distance(price, ma5),
        "ma20_distance": _distance(price, ma20),
        "momentum_5d": closes[-1] / closes[-5] - 1,
        "momentum_20d": closes[-1] / closes[0] - 1,
        "volatility_20d": statistics.pstdev(returns),
        "average_amount_20d_log": math.log1p(
            statistics.fmean(float(item.amount) for item in completed[-20:])
        ),
        "benchmark_ma5_distance": _distance(
            benchmark_closes[-1], statistics.fmean(benchmark_closes)
        ),
        "relative_strength": intraday_return - benchmark_return,
        "market_breadth": float(market_breadth),
    }
    if tuple(features) != FEATURE_NAMES:
        raise RuntimeError("概率组合特征契约顺序发生变化")
    return FeatureVectorResult(True, features, ())
