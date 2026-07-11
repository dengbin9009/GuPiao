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


def test_live_runtime_requires_both_environment_switches():
    from app.config import Settings, live_runtime_is_open

    assert not live_runtime_is_open(
        Settings(live_enabled=False, broker_adapter="qmt")
    )
    assert not live_runtime_is_open(
        Settings(live_enabled=True, broker_adapter="simulation")
    )
    assert live_runtime_is_open(
        SimpleNamespace(live_enabled=True, broker_adapter="qmt")
    )


def test_live_api_mode_enable_is_blocked_by_runtime_gate(db=None):
    from fastapi import HTTPException

    from app.main import LiveModeUpdate, update_live_mode

    with pytest.raises(HTTPException) as exc_info:
        update_live_mode(
            LiveModeUpdate(enabled=True, confirmation="ENABLE LIVE"),
            None,
            db,
        )

    assert exc_info.value.status_code == 403


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


def test_schedule_does_not_run_before_target_time():
    current = datetime(2026, 6, 22, 14, 39, 40, tzinfo=ZoneInfo("Asia/Shanghai"))

    decision = evaluate_schedule(
        trigger_type="entry_evaluation",
        run_time="14:40:00",
        enabled=True,
        last_scheduled_for=None,
        current=current,
    )

    assert not decision.should_run
    assert decision.reason == "不在有效执行窗口"


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


def test_scheduler_only_retries_explicit_transient_data_failures():
    from app.scheduler_runner import (
        retry_is_due,
        schedule_run_needs_retry,
        schedule_tolerance_seconds,
    )

    assert schedule_run_needs_retry(
        SimpleNamespace(status="completed", summary={"retryable": True})
    )
    assert not schedule_run_needs_retry(
        SimpleNamespace(status="completed", summary={"accepted": 0, "reason": "没有候选股"})
    )
    assert not schedule_run_needs_retry(
        SimpleNamespace(status="failed", summary={})
    )
    current = datetime(2026, 7, 13, 14, 40, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert retry_is_due(None, current=current)
    assert not retry_is_due(
        current.replace(second=20),
        current=current,
    )
    assert retry_is_due(
        current.replace(second=5, tzinfo=None),
        current=current,
    )
    exit_schedule = SimpleNamespace(
        trigger_type="exit_evaluation",
        run_time="09:35:00",
    )
    config = SimpleNamespace(parameters={"latest_exit_time": "10:00:00"})
    assert schedule_tolerance_seconds(exit_schedule, config) == 1500


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
