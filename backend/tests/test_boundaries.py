from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app import notifications
from app.brokers import DisabledBrokerAdapter, build_broker_adapter
from app.scheduler import evaluate_schedule


def test_unconfigured_broker_fails_closed():
    adapter = DisabledBrokerAdapter("LIVE")
    assert not adapter.health().healthy
    assert adapter.query_accounts() == []
    with pytest.raises(RuntimeError):
        adapter.place_order({"symbol": "000001.SZ"})


def test_qmt_without_url_is_unhealthy():
    adapter = build_broker_adapter("qmt")
    assert not adapter.health().healthy


def test_schedule_is_idempotent_and_time_bounded():
    current = datetime(2026, 6, 22, 14, 40, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    first = evaluate_schedule(
        trigger_type="entry_evaluation",
        run_time="14:40:00",
        enabled=True,
        last_scheduled_for=None,
        current=current,
    )
    assert first.should_run
    duplicate = evaluate_schedule(
        trigger_type="entry_evaluation",
        run_time="14:40:00",
        enabled=True,
        last_scheduled_for=first.window_key,
        current=current,
    )
    assert not duplicate.should_run
    late = evaluate_schedule(
        trigger_type="entry_evaluation",
        run_time="14:40:00",
        enabled=True,
        last_scheduled_for=None,
        current=current.replace(hour=14, minute=45),
    )
    assert not late.should_run


def test_schedule_uses_calendar_provider_when_supplied():
    current = datetime(2026, 6, 22, 14, 40, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
    denied = evaluate_schedule(
        trigger_type="entry_evaluation",
        run_time="14:40:00",
        enabled=True,
        last_scheduled_for=None,
        current=current,
        trading_day_fn=lambda _: False,
    )
    assert not denied.should_run
    assert denied.reason == "非交易日"


def test_notification_delivery_retries_up_to_success():
    attempts = 0

    def flaky_sender():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("temporary failure")

    result = notifications.deliver_with_retries(flaky_sender, max_attempts=3)

    assert result.sent
    assert result.attempt_count == 3
    assert result.last_error is None


def test_risk_engine_blocks_excessive_total_exposure():
    from app.risk import evaluate_order

    settings = SimpleNamespace(
        emergency_stop_enabled=False,
        max_order_notional_abs=5000,
        max_order_notional_pct=0.50,
        max_position_pct=0.50,
        max_total_exposure_pct=0.60,
        daily_loss_limit_pct=0.03,
        max_consecutive_errors=3,
    )

    decision = evaluate_order(
        settings,
        order_notional=2000,
        total_asset=10000,
        position_market_value=1000,
        total_market_value=5000,
        daily_pnl_pct=0,
        consecutive_errors=0,
    )

    assert not decision.allowed
    assert decision.code == "max_total_exposure"
