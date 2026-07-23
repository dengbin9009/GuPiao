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
        configured = False
        for provider in self.providers:
            if "trading_calendar" not in getattr(provider, "capabilities", set()):
                continue
            configured = True
            healthy, error = provider.health()
            if not healthy:
                failures.append(
                    f"{getattr(provider, 'name', 'unknown')}: {error or 'unhealthy'}"
                )
                continue
            attempted = True
            try:
                return list(provider.trading_days(start=start_str, end=end_str))
            except Exception as exc:
                failures.append(f"{getattr(provider, 'name', 'unknown')}: {exc}")
                continue
        if attempted and failures:
            raise MarketDataError(f"交易日历获取失败: {'; '.join(failures)}")
        if configured and failures:
            raise MarketDataError(f"交易日历不可用: {'; '.join(failures)}")
        return []

    def is_trading_day(self, day: date, *, allow_weekday_fallback: bool = True) -> bool:
        try:
            days = self.trading_days(start=day, end=day)
        except MarketDataError:
            return day.weekday() < 5 if allow_weekday_fallback else False
        if days:
            return day.isoformat() in days
        return day.weekday() < 5 if allow_weekday_fallback else False
