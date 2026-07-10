from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from .market_data import MarketDataError


class TradingCalendarService:
    def __init__(self, providers: Iterable[Any]):
        self.providers = list(providers)

    def trading_days(self, *, start: date, end: date) -> list[str]:
        start_str = start.isoformat()
        end_str = end.isoformat()
        failures: list[str] = []
        attempted = False
        for provider in self.providers:
            if "trading_calendar" not in getattr(provider, "capabilities", set()):
                continue
            healthy, error = provider.health()
            if not healthy:
                continue
            attempted = True
            try:
                return list(provider.trading_days(start=start_str, end=end_str))
            except Exception as exc:
                failures.append(f"{getattr(provider, 'name', 'unknown')}: {exc}")
                continue
        if attempted and failures:
            raise MarketDataError(f"交易日历获取失败: {'; '.join(failures)}")
        return []

    def is_trading_day(self, day: date) -> bool:
        days = self.trading_days(start=day, end=day)
        if days:
            return day.isoformat() in days
        return day.weekday() < 5
