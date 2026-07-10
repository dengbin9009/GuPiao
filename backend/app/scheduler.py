from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


def is_trading_day(day: date) -> bool:
    """Weekday fallback. Provider calendars replace this in production."""
    return day.weekday() < 5


@dataclass(frozen=True)
class ScheduleDecision:
    should_run: bool
    window_key: str
    reason: str


def evaluate_schedule(
    *,
    trigger_type: str,
    run_time: str,
    enabled: bool,
    last_scheduled_for: str | None,
    current: datetime | None = None,
    tolerance_seconds: int = 59,
    trading_day_fn=is_trading_day,
) -> ScheduleDecision:
    current = (current or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    window_key = f"{current.date().isoformat()}:{trigger_type}:{run_time}"
    if not enabled:
        return ScheduleDecision(False, window_key, "调度未启用")
    if not trading_day_fn(current.date()):
        return ScheduleDecision(False, window_key, "非交易日")
    if last_scheduled_for == window_key:
        return ScheduleDecision(False, window_key, "本窗口已执行")
    target = time.fromisoformat(run_time)
    seconds = abs(
        (current.hour * 3600 + current.minute * 60 + current.second)
        - (target.hour * 3600 + target.minute * 60 + target.second)
    )
    if seconds > tolerance_seconds:
        return ScheduleDecision(False, window_key, "不在有效执行窗口")
    return ScheduleDecision(True, window_key, "允许执行")
