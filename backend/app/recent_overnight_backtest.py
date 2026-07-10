from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .market_data import ProviderRouter
from .market_cache import bar_cache_coverage, merge_bar_rows, read_bar_cache, write_bar_cache


@dataclass(frozen=True)
class CoverageWindows:
    entry_start: str
    entry_end: str
    exit_start: str
    exit_end: str


def _windows(entry_date: str, exit_date: str, entry_time: str, exit_time: str) -> CoverageWindows:
    return CoverageWindows(
        entry_start=f"{entry_date}T{entry_time}:00",
        entry_end=f"{entry_date}T{entry_time}:59",
        exit_start=f"{exit_date}T{exit_time}:00",
        exit_end=f"{exit_date}T{exit_time}:59",
    )


def _hour_windows(entry_date: str, exit_date: str) -> CoverageWindows:
    return CoverageWindows(
        entry_start=f"{entry_date}T14:00:00",
        entry_end=f"{entry_date}T15:59:59",
        exit_start=f"{exit_date}T09:00:00",
        exit_end=f"{exit_date}T10:59:59",
    )


def _select_bar(
    rows: list[dict[str, Any]],
    *,
    start: str,
    end: str,
    selection: str = "first",
) -> dict[str, Any]:
    matches = [
        row
        for row in sorted(rows, key=lambda item: str(item.get("timestamp", "")))
        if start <= str(row.get("timestamp", "")) <= end
    ]
    if matches:
        return matches[-1] if selection == "last" else matches[0]
    raise ValueError(f"分钟线覆盖不足: {start} - {end}")


def _contains_demo_provider(rows: list[dict[str, Any]]) -> bool:
    providers = {str(row.get("provider", "")).lower() for row in rows}
    return any("demo" in provider for provider in providers if provider)


def _result_summary(
    *,
    symbol: str,
    entry_bar: dict[str, Any],
    exit_bar: dict[str, Any],
    quantity: int,
    initial_cash: float,
    commission_rate: float,
    min_commission: float,
    stamp_tax_rate: float,
    transfer_fee_rate: float,
    slippage_bps: float,
    data_source: str,
    coverage: dict[str, Any],
    timeframe_used: str,
) -> dict[str, Any]:
    entry_close = float(entry_bar["close"])
    exit_close = float(exit_bar["close"])
    entry_price = entry_close * (1 + slippage_bps / 10_000)
    exit_price = exit_close * (1 - slippage_bps / 10_000)
    buy_notional = entry_price * quantity
    sell_notional = exit_price * quantity
    buy_commission = max(buy_notional * commission_rate, min_commission)
    sell_commission = max(sell_notional * commission_rate, min_commission)
    stamp_tax = sell_notional * stamp_tax_rate
    transfer_fee = sell_notional * transfer_fee_rate
    gross_pnl = sell_notional - buy_notional
    net_pnl = gross_pnl - buy_commission - sell_commission - stamp_tax - transfer_fee
    return_pct = net_pnl / initial_cash if initial_cash else 0.0
    return {
        "symbol": symbol,
        "timeframe_used": timeframe_used,
        "data_source": data_source,
        "coverage": coverage,
        "entry": {"timestamp": str(entry_bar["timestamp"]), "price": round(entry_price, 4), "close": entry_close},
        "exit": {"timestamp": str(exit_bar["timestamp"]), "price": round(exit_price, 4), "close": exit_close},
        "quantity": quantity,
        "initial_cash": initial_cash,
        "gross_pnl": round(gross_pnl, 4),
        "commission": round(buy_commission + sell_commission, 4),
        "stamp_tax": round(stamp_tax, 4),
        "transfer_fee": round(transfer_fee, 4),
        "slippage_amount": round((entry_price - entry_close) * quantity + (exit_close - exit_price) * quantity, 4),
        "net_pnl": round(net_pnl, 4),
        "return_pct": round(return_pct, 6),
    }


def _fetch_rows(provider: Any, *, symbol: str, entry_date: str, exit_date: str, timeframe: str, capability: str) -> list[dict[str, Any]]:
    if isinstance(provider, ProviderRouter):
        result = provider.call(capability, "bars", symbol=symbol, timeframe=timeframe, start=entry_date, end=exit_date)
        return list(result.data)
    return provider.bars(symbol=symbol, timeframe=timeframe, start=entry_date, end=exit_date)


def run_recent_overnight_backtest(
    *,
    symbol: str,
    entry_date: str,
    exit_date: str,
    cache_root: Path,
    provider: Any | None,
    initial_cash: float = 10_000,
    commission_rate: float = 0.0003,
    min_commission: float = 5,
    stamp_tax_rate: float = 0.0005,
    transfer_fee_rate: float = 0.0,
    slippage_bps: float = 5,
    entry_time: str = "14:45",
    exit_time: str = "09:35",
    preferred_timeframe: str = "1m",
) -> dict[str, Any]:
    attempts = [preferred_timeframe]
    if preferred_timeframe == "1m":
        attempts.append("60m")
    last_error: Exception | None = None
    primary_error: Exception | None = None
    for timeframe in attempts:
        cache_path = cache_root / f"{symbol}-{timeframe}.parquet"
        windows = _windows(entry_date, exit_date, entry_time, exit_time) if timeframe == "1m" else _hour_windows(entry_date, exit_date)
        coverage = bar_cache_coverage(
            cache_path,
            entry_start=windows.entry_start,
            entry_end=windows.entry_end,
            exit_start=windows.exit_start,
            exit_end=windows.exit_end,
        )
        data_source = "cache"
        if not coverage["complete"]:
            if provider is None:
                error = ValueError(f"{timeframe} 数据覆盖不足: {symbol} {entry_date}->{exit_date}")
                if timeframe == preferred_timeframe and primary_error is None:
                    primary_error = error
                last_error = error
                continue
            try:
                incoming = _fetch_rows(
                    provider,
                    symbol=symbol,
                    entry_date=entry_date,
                    exit_date=exit_date,
                    timeframe=timeframe,
                    capability="minute" if timeframe == "1m" else "hour",
                )
            except Exception as exc:
                if timeframe == preferred_timeframe and primary_error is None:
                    primary_error = exc
                last_error = exc
                continue
            merged = merge_bar_rows(read_bar_cache(cache_path), incoming)
            write_bar_cache(cache_path, merged)
            coverage = bar_cache_coverage(
                cache_path,
                entry_start=windows.entry_start,
                entry_end=windows.entry_end,
                exit_start=windows.exit_start,
                exit_end=windows.exit_end,
            )
            coverage["fetched"] = True
            data_source = "provider+cache"
        if not coverage["complete"]:
            error = ValueError(f"{timeframe} 数据覆盖不足: {symbol} {entry_date}->{exit_date}")
            if timeframe == preferred_timeframe and primary_error is None:
                primary_error = error
            last_error = error
            continue
        rows = read_bar_cache(cache_path)
        if _contains_demo_provider(rows):
            error = ValueError(f"演示{timeframe}数据不可用于真实隔夜回测: {symbol}")
            if timeframe == preferred_timeframe and primary_error is None:
                primary_error = error
            last_error = error
            continue
        entry_bar = _select_bar(
            rows,
            start=windows.entry_start,
            end=windows.entry_end,
            selection="last" if timeframe == "60m" else "first",
        )
        exit_bar = _select_bar(rows, start=windows.exit_start, end=windows.exit_end)
        entry_price = float(entry_bar["close"])
        quantity = int(initial_cash // entry_price // 100) * 100
        if quantity <= 0:
            raise ValueError("初始资金不足一手")
        coverage.setdefault("fetched", False)
        return _result_summary(
            symbol=symbol,
            entry_bar=entry_bar,
            exit_bar=exit_bar,
            quantity=quantity,
            initial_cash=initial_cash,
            commission_rate=commission_rate,
            min_commission=min_commission,
            stamp_tax_rate=stamp_tax_rate,
            transfer_fee_rate=transfer_fee_rate,
            slippage_bps=slippage_bps,
            data_source=data_source,
            coverage=coverage,
            timeframe_used=timeframe,
        )
    if preferred_timeframe == "1m" and primary_error and last_error is not primary_error:
        raise ValueError(f"1m 不可用: {primary_error}; 60m 也不可用: {last_error}")
    if primary_error:
        raise ValueError(str(primary_error))
    if last_error:
        raise ValueError(str(last_error))
    raise ValueError(f"无法获取可用行情数据: {symbol} {entry_date}->{exit_date}")
