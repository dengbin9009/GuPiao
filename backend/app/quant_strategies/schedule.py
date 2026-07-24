from __future__ import annotations

from datetime import date, timedelta
from typing import Callable


def adjacent_trading_day(
    current: date,
    *,
    direction: int,
    trading_day_fn: Callable[[date], bool],
    max_calendar_days: int = 20,
) -> date | None:
    if direction not in {-1, 1}:
        raise ValueError("交易日方向只能为前一日或后一日")
    for offset in range(1, max_calendar_days + 1):
        candidate = current + timedelta(days=direction * offset)
        if trading_day_fn(candidate):
            return candidate
    return None


def should_generate_signal(
    frequency: str,
    current: date,
    *,
    next_trading_day: date,
) -> bool:
    if frequency in {"daily", "event"}:
        return True
    if frequency == "weekly":
        return current.isocalendar()[:2] != next_trading_day.isocalendar()[:2]
    if frequency == "monthly":
        return (current.year, current.month) != (
            next_trading_day.year,
            next_trading_day.month,
        )
    raise ValueError(f"未知调仓周期: {frequency}")
