from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any


def write_bar_cache(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import pandas as pd

        pd.DataFrame(rows).to_parquet(path, index=False)
    except Exception:
        lines = ["timestamp,open,high,low,close,volume,amount"]
        lines.extend(
            f"{row['timestamp']},{row['open']},{row['high']},{row['low']},{row['close']},{row['volume']},{row['amount']}"
            for row in rows
        )
        path.write_text("\n".join(lines), encoding="utf-8")


def read_bar_cache(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        import pandas as pd

        return list(pd.read_parquet(path).to_dict(orient="records"))
    except Exception:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= 1:
            return []
        result: list[dict[str, Any]] = []
        for line in lines[1:]:
            timestamp, open_, high, low, close, volume, amount = line.split(",")
            result.append(
                {
                    "timestamp": timestamp,
                    "open": float(open_),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                    "volume": int(volume),
                    "amount": float(amount),
                }
            )
        return result


def refresh_bar_cache(
    root: Path,
    provider: Any,
    *,
    symbol: str,
    timeframe: str,
    start: str | None = None,
    end: str | None = None,
) -> Path:
    rows = provider.bars(symbol=symbol, timeframe=timeframe, start=start, end=end)
    path = root / f"{symbol}-{timeframe}.parquet"
    write_bar_cache(path, rows)
    return path


def merge_bar_rows(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_timestamp: dict[str, dict[str, Any]] = {}
    for row in existing + incoming:
        timestamp = str(row.get("timestamp", ""))
        if not timestamp:
            continue
        by_timestamp[timestamp] = dict(row)
    return [by_timestamp[key] for key in sorted(by_timestamp)]


def bar_cache_coverage(
    path: Path,
    *,
    entry_start: str,
    entry_end: str,
    exit_start: str,
    exit_end: str,
) -> dict[str, Any]:
    rows = read_bar_cache(path)
    timestamps = {str(row.get("timestamp", "")) for row in rows}
    entry_covered = any(entry_start <= item <= entry_end for item in timestamps)
    exit_covered = any(exit_start <= item <= exit_end for item in timestamps)
    return {
        "rows": len(rows),
        "entry_covered": entry_covered,
        "exit_covered": exit_covered,
        "complete": entry_covered and exit_covered,
    }


def quote_is_stale(
    quote_at: datetime | None,
    *,
    current: datetime | None = None,
    stale_after_seconds: int,
) -> bool:
    if quote_at is None:
        return True
    current = current or datetime.now().astimezone()
    if quote_at.tzinfo is None:
        quote_at = quote_at.replace(tzinfo=current.tzinfo)
    return (current - quote_at).total_seconds() > stale_after_seconds
