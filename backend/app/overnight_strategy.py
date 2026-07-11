from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable


@dataclass(frozen=True)
class CandidateResult:
    accepted: list[dict[str, Any]]
    rejected: list[dict[str, Any]]


@dataclass(frozen=True)
class OvernightSelectionResult:
    selected: dict[str, Any] | None
    accepted: list[dict[str, Any]]
    rejected: list[dict[str, Any]]


def build_universe_candidates(
    stocks: Iterable[Any],
    *,
    current: datetime,
    include_bse: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    min_listing_days = 120
    for stock in stocks:
        exchange = str(getattr(stock, "exchange", "") or "")
        if exchange == "BSE" and not include_bse:
            continue
        symbol = str(getattr(stock, "symbol", "") or "")
        code = str(getattr(stock, "code", "") or symbol.split(".")[0])
        created_at = getattr(stock, "created_at", None)
        listing_days = min_listing_days
        if isinstance(created_at, datetime):
            created = created_at if created_at.tzinfo else created_at.replace(tzinfo=current.tzinfo)
            listing_days = max((current - created).days, min_listing_days)
        last_price = float(getattr(stock, "last_price", 0) or 0)
        change_pct = float(getattr(stock, "change_pct", 0) or 0)
        intraday_return = change_pct / 100
        rows.append(
            {
                "symbol": symbol,
                "code": code,
                "name": str(getattr(stock, "name", "") or ""),
                "exchange": exchange,
                "status": str(getattr(stock, "status", "active") or "active"),
                "listing_days": listing_days,
                "turnover_amount": float(getattr(stock, "turnover_amount", 0) or 0),
                "turnover_rate": float(getattr(stock, "turnover_rate", 0.02) or 0.02),
                "intraday_return": intraday_return,
                "above_vwap": last_price > 0,
                "above_ma5": last_price > 0,
                "tradable": last_price > 0 and exchange in {"SSE", "SZSE", "BSE"},
                "price": last_price,
                "quote_updated_at": getattr(stock, "quote_updated_at", None),
            }
        )
    return rows


def evaluate_candidates(
    candidates: Iterable[dict[str, Any]],
    parameters: dict[str, Any],
    *,
    critical_event_symbols: set[str],
    benchmark_above_ma5: bool = True,
) -> CandidateResult:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        reasons: list[str] = []
        symbol = str(candidate.get("symbol", ""))
        name = str(candidate.get("name", ""))
        if candidate.get("exchange") == "BSE" and not parameters.get("include_bse", False):
            reasons.append("北交所未纳入股票池")
        if candidate.get("status") != "active" or not candidate.get("tradable", False):
            reasons.append("股票当前不可交易")
        if parameters.get("exclude_st", True) and "ST" in name.upper():
            reasons.append("ST 股票已排除")
        if int(candidate.get("listing_days", 0)) < int(parameters.get("min_listing_days", 60)):
            reasons.append("上市时间不足")
        if float(candidate.get("turnover_amount", 0)) < float(parameters.get("min_turnover_amount", 0)):
            reasons.append("成交额不足")
        if float(candidate.get("turnover_rate", 0)) < float(parameters.get("min_turnover_rate", 0)):
            reasons.append("换手率不足")
        intraday_return = float(candidate.get("intraday_return", 0))
        if not float(parameters.get("min_intraday_return", 0)) <= intraday_return <= float(
            parameters.get("max_intraday_return", 1)
        ):
            reasons.append("日内涨幅不在策略范围")
        if not candidate.get("above_vwap", False):
            reasons.append("价格未站上日内 VWAP")
        if not candidate.get("above_ma5", False):
            reasons.append("价格未站上五日均线")
        if not benchmark_above_ma5:
            reasons.append("市场基准未通过")
        if parameters.get("event_risk_enabled", True) and symbol in critical_event_symbols:
            reasons.append("命中重大事件风险")
        if reasons:
            rejected.append({"symbol": symbol, "reasons": reasons})
        else:
            accepted.append(dict(candidate))
    accepted.sort(
        key=lambda item: (float(item.get("intraday_return", 0)), float(item.get("turnover_amount", 0))),
        reverse=True,
    )
    limit = max(1, int(parameters.get("max_candidates", 3)))
    for overflow in accepted[limit:]:
        rejected.append({"symbol": overflow["symbol"], "reasons": ["超过最大候选数量"]})
    return CandidateResult(accepted=accepted[:limit], rejected=rejected)


def select_best_candidate(
    candidates: Iterable[dict[str, Any]],
    parameters: dict[str, Any],
    *,
    critical_event_symbols: set[str],
    benchmark_above_ma5: bool = True,
) -> OvernightSelectionResult:
    result = evaluate_candidates(
        candidates,
        parameters,
        critical_event_symbols=critical_event_symbols,
        benchmark_above_ma5=benchmark_above_ma5,
    )
    selected = dict(result.accepted[0]) if result.accepted else None
    return OvernightSelectionResult(selected=selected, accepted=result.accepted, rejected=result.rejected)


def calculate_position_quantity(
    *,
    equity: float,
    price: float,
    target_pct: float,
    risk_notional: float,
) -> int:
    if equity <= 0 or price <= 0 or target_pct <= 0 or risk_notional <= 0:
        return 0
    notional = min(equity * target_pct, risk_notional)
    return math.floor(notional / price / 100) * 100
