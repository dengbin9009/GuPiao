from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PrefilterResult:
    candidates: list[dict[str, Any]]
    holdings: list[str]
    required_symbols: list[str]
    rejected: dict[str, str]


@dataclass(frozen=True)
class Snapshot:
    payload: str
    sha256: str


def build_snapshot(value: Any) -> Snapshot:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return Snapshot(
        payload=payload,
        sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )


def _rank_percent(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    denominator = max(1, len(ordered) - 1)
    return {
        symbol: index / denominator
        for index, (symbol, _value) in enumerate(ordered)
    }


def _metrics(row: dict[str, Any], *, as_of: str) -> tuple[dict[str, float] | None, str | None]:
    bars = sorted(row.get("bars") or [], key=lambda item: str(item.get("trade_date", "")))
    if len(bars) < 60:
        return None, "日线不足60根"
    if any(str(item.get("trade_date", "")) > as_of for item in bars):
        return None, "日线包含未来数据"
    closes = [float(item.get("close", 0) or 0) for item in bars[-60:]]
    amounts = [float(item.get("amount", 0) or 0) for item in bars[-20:]]
    if min(closes) <= 0:
        return None, "日线价格无效"
    ma20 = statistics.fmean(closes[-20:])
    momentum20 = closes[-1] / closes[-21] - 1
    momentum5 = closes[-1] / closes[-6] - 1
    returns = [closes[index] / closes[index - 1] - 1 for index in range(41, 60)]
    volatility20 = statistics.pstdev(returns) if returns else 0
    average_amount20 = statistics.fmean(amounts)
    if momentum20 <= 0:
        return None, "20日趋势未转正"
    if closes[-1] <= ma20:
        return None, "价格未站上MA20"
    if average_amount20 < 100_000_000:
        return None, "20日平均成交额不足"
    return {
        "momentum20": momentum20,
        "momentum5": momentum5,
        "average_amount20": average_amount20,
        "today_amount": float(row.get("turnover_amount", 0) or 0),
        "stability": -volatility20,
        "ma20": ma20,
    }, None


def select_candidates(
    rows: list[dict[str, Any]],
    *,
    as_of: str,
    prefilter_size: int,
    top_n: int,
    critical_event_symbols: set[str],
) -> PrefilterResult:
    holdings = sorted(
        str(row["symbol"])
        for row in rows
        if row.get("is_holding")
    )
    rejected: dict[str, str] = {}
    liquid: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol", ""))
        if row.get("is_holding"):
            continue
        if row.get("exchange") not in {"SSE", "SZSE"}:
            rejected[symbol] = "市场未纳入"
        elif row.get("status") != "active" or float(row.get("last_price", 0) or 0) <= 0:
            rejected[symbol] = "股票当前不可交易"
        elif "ST" in str(row.get("name", "")).upper():
            rejected[symbol] = "ST股票已排除"
        elif symbol in critical_event_symbols:
            rejected[symbol] = "命中重大事件风险"
        else:
            liquid.append(row)
    liquid.sort(
        key=lambda item: (
            -float(item.get("turnover_amount", 0) or 0),
            str(item.get("symbol", "")),
        )
    )
    liquid = liquid[: max(1, int(prefilter_size))]

    metrics_by_symbol: dict[str, dict[str, float]] = {}
    rows_by_symbol: dict[str, dict[str, Any]] = {}
    for row in liquid:
        symbol = str(row["symbol"])
        metrics, reason = _metrics(row, as_of=as_of)
        if reason:
            rejected[symbol] = reason
            continue
        metrics_by_symbol[symbol] = metrics or {}
        rows_by_symbol[symbol] = row

    ranks = {
        key: _rank_percent(
            {symbol: values[key] for symbol, values in metrics_by_symbol.items()}
        )
        for key in (
            "momentum20",
            "momentum5",
            "average_amount20",
            "today_amount",
            "stability",
        )
    }
    candidates: list[dict[str, Any]] = []
    for symbol, metrics in metrics_by_symbol.items():
        score = (
            0.35 * ranks["momentum20"][symbol]
            + 0.20 * ranks["momentum5"][symbol]
            + 0.20 * ranks["average_amount20"][symbol]
            + 0.15 * ranks["today_amount"][symbol]
            + 0.10 * ranks["stability"][symbol]
        )
        candidates.append(
            {
                **rows_by_symbol[symbol],
                "metrics": metrics,
                "score": score,
            }
        )
    candidates.sort(
        key=lambda item: (
            -float(item["score"]),
            -float(item.get("turnover_amount", 0) or 0),
            str(item["symbol"]),
        )
    )
    selected = candidates[: max(1, int(top_n))]
    selected_symbols = [str(item["symbol"]) for item in selected]
    required = selected_symbols + [symbol for symbol in holdings if symbol not in selected_symbols]
    return PrefilterResult(
        candidates=selected,
        holdings=holdings,
        required_symbols=required,
        rejected=rejected,
    )
