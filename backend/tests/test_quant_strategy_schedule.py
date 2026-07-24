from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.quant_strategies.schedule import should_generate_signal
from app.worker import (
    event_poll_scope,
    poll_due_quant_data_sync,
    quant_data_sync_scope,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_signal_frequency_uses_next_trading_day_boundary():
    friday = date(2026, 7, 24)
    next_monday = date(2026, 7, 27)
    month_end = date(2026, 7, 31)
    next_month = date(2026, 8, 3)

    assert should_generate_signal("daily", friday, next_trading_day=next_monday)
    assert should_generate_signal("event", friday, next_trading_day=next_monday)
    assert should_generate_signal("weekly", friday, next_trading_day=next_monday)
    assert not should_generate_signal("monthly", friday, next_trading_day=next_monday)
    assert should_generate_signal("monthly", month_end, next_trading_day=next_month)
    assert not should_generate_signal(
        "weekly",
        date(2026, 7, 23),
        next_trading_day=friday,
    )


def test_quant_data_sync_retries_until_the_first_signal_window_on_weekdays():
    assert quant_data_sync_scope(datetime(2026, 7, 24, 16, 15, 0, tzinfo=SHANGHAI))
    assert quant_data_sync_scope(datetime(2026, 7, 24, 16, 29, 59, tzinfo=SHANGHAI))
    assert not quant_data_sync_scope(datetime(2026, 7, 24, 16, 30, 0, tzinfo=SHANGHAI))
    assert not quant_data_sync_scope(datetime(2026, 7, 25, 16, 15, 0, tzinfo=SHANGHAI))


def test_event_poll_scope_covers_quant_signal_preheat_window():
    assert event_poll_scope(datetime(2026, 7, 24, 9, 30, tzinfo=SHANGHAI))
    assert event_poll_scope(datetime(2026, 7, 24, 9, 59, 59, tzinfo=SHANGHAI))
    assert not event_poll_scope(datetime(2026, 7, 24, 10, 0, tzinfo=SHANGHAI))
    assert event_poll_scope(datetime(2026, 7, 24, 16, 10, tzinfo=SHANGHAI))
    assert event_poll_scope(datetime(2026, 7, 24, 16, 29, 59, tzinfo=SHANGHAI))
    assert not event_poll_scope(datetime(2026, 7, 24, 16, 30, tzinfo=SHANGHAI))


class FakeFuture:
    def __init__(self, *, completed=False, result=None):
        self.completed = completed
        self.value = result
        self.result_calls = 0

    def done(self):
        return self.completed

    def result(self):
        self.result_calls += 1
        return self.value


def test_quant_data_sync_is_submitted_without_waiting_for_result():
    future = FakeFuture(completed=False)
    submitted = []
    current = datetime(2026, 7, 24, 16, 15, tzinfo=SHANGHAI)

    state = poll_due_quant_data_sync(
        current=current,
        current_seconds=100,
        future=None,
        last_sync_date=None,
        last_attempt_seconds=None,
        submit=lambda value: submitted.append(value) or future,
    )

    assert submitted == [current]
    assert state.future is future
    assert state.started is True
    assert state.result is None
    assert future.result_calls == 0


def test_quant_data_sync_does_not_submit_twice_while_running():
    future = FakeFuture(completed=False)
    submitted = []

    state = poll_due_quant_data_sync(
        current=datetime(2026, 7, 24, 16, 16, tzinfo=SHANGHAI),
        current_seconds=200,
        future=future,
        last_sync_date=None,
        last_attempt_seconds=100,
        submit=lambda value: submitted.append(value),
    )

    assert submitted == []
    assert state.future is future
    assert state.started is False
    assert future.result_calls == 0


def test_quant_data_sync_records_date_only_after_successful_completion():
    result = {"stocks": 800, "etfs": 6, "daily_rows": 806, "errors": 0}
    future = FakeFuture(completed=True, result=result)
    current = datetime(2026, 7, 24, 16, 21, tzinfo=SHANGHAI)

    state = poll_due_quant_data_sync(
        current=current,
        current_seconds=500,
        future=future,
        last_sync_date=None,
        last_attempt_seconds=100,
        submit=lambda _value: None,
    )

    assert state.future is None
    assert state.last_sync_date == current.date()
    assert state.result == result
    assert state.started is False
    assert future.result_calls == 1
