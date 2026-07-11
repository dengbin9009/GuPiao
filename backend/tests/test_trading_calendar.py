from __future__ import annotations

from datetime import date


def test_schedule_skips_calendar_lookup_outside_execution_window():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.scheduler import evaluate_schedule

    calls = 0

    def trading_day_fn(_):
        nonlocal calls
        calls += 1
        return True

    decision = evaluate_schedule(
        trigger_type="entry_evaluation",
        run_time="14:40",
        enabled=True,
        last_scheduled_for=None,
        current=datetime(2026, 7, 10, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        trading_day_fn=trading_day_fn,
    )

    assert not decision.should_run
    assert calls == 0


class CalendarProvider:
    name = "calendar-provider"
    capabilities = frozenset({"trading_calendar"})

    def __init__(self, healthy=True, days=None):
        self._healthy = healthy
        self._days = days or []

    def health(self):
        return self._healthy, None if self._healthy else "offline"

    def trading_days(self, *, start: str, end: str):
        assert start <= end
        return self._days


def test_trading_calendar_prefers_provider_days():
    from app.trading_calendar import TradingCalendarService

    service = TradingCalendarService([CalendarProvider(days=["2026-06-22", "2026-06-23"])])

    assert service.is_trading_day(date(2026, 6, 22))
    assert not service.is_trading_day(date(2026, 6, 21))


def test_trading_calendar_falls_back_to_weekdays_when_provider_unavailable():
    from app.trading_calendar import TradingCalendarService

    service = TradingCalendarService([CalendarProvider(healthy=False)])

    assert service.is_trading_day(date(2026, 6, 22))
    assert not service.is_trading_day(date(2026, 6, 21))


def test_trading_calendar_falls_back_to_next_provider_when_first_provider_errors():
    from app.trading_calendar import TradingCalendarService

    class BrokenProvider(CalendarProvider):
        def trading_days(self, *, start: str, end: str):
            raise RuntimeError("calendar crashed")

    service = TradingCalendarService(
        [
            BrokenProvider(days=[]),
            CalendarProvider(days=["2026-06-22"]),
        ]
    )

    assert service.is_trading_day(date(2026, 6, 22))


def test_trading_calendar_falls_back_to_weekdays_when_all_providers_error():
    from app.trading_calendar import TradingCalendarService

    class BrokenProvider(CalendarProvider):
        def trading_days(self, *, start: str, end: str):
            raise RuntimeError("calendar crashed")

    service = TradingCalendarService([BrokenProvider()])

    assert service.is_trading_day(date(2026, 6, 22))
    assert not service.is_trading_day(date(2026, 6, 21))


def test_trading_calendar_fails_closed_for_live_mode_when_all_providers_error():
    from app.trading_calendar import TradingCalendarService

    class BrokenProvider(CalendarProvider):
        def trading_days(self, *, start: str, end: str):
            raise RuntimeError("calendar crashed")

    service = TradingCalendarService([BrokenProvider()])

    assert not service.is_trading_day(date(2026, 6, 22), allow_weekday_fallback=False)
