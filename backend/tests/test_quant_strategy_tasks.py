from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database import Base
from app.models import (
    DataSourceState,
    MarketDailyBar,
    MarketDailyMetric,
    NotificationChannel,
    NotificationDelivery,
    QuantStrategyTask,
    RiskSettings,
    StrategyRiskProfile,
    StrategyConfig,
    StrategyDefinition,
    StrategySchedule,
    Stock,
    FinancialReportSnapshot,
)
from app.quant_strategies.readiness import (
    quant_dataset_state_reasons,
    quant_strategy_readiness,
)
from app.quant_strategies.runtime import seed_quant_strategy_runtimes
from app.quant_strategies.tasks import claim_pending_task, enqueue_task, fail_task
from app.services import seed_database


SHANGHAI = ZoneInfo("Asia/Shanghai")


def seeded_database(tmp_path: Path):
    database_url = f"sqlite:///{tmp_path / 'tasks.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    settings = Settings(database_url=database_url, live_enabled=False, broker_adapter="simulation")
    with Session(engine) as db:
        seed_database(db, settings)
        configs = seed_quant_strategy_runtimes(db, settings)
        config_id = configs["multi_factor_core"].id
        for schedule in db.scalars(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config_id
            )
        ):
            schedule.enabled = True
        db.commit()
    return engine, settings, config_id


def test_enqueue_task_is_idempotent_for_strategy_date_and_type(tmp_path: Path):
    engine, _, config_id = seeded_database(tmp_path)
    with Session(engine) as db:
        first = enqueue_task(db, config_id, "signal", "2026-07-24")
        second = enqueue_task(db, config_id, "signal", "2026-07-24")

        assert first.id == second.id
        assert first.status == "pending"
        assert len(list(db.scalars(select(QuantStrategyTask)))) == 1


@pytest.mark.parametrize("task_type", ["dry_run", "performance", "unknown"])
def test_enqueue_rejects_task_types_without_a_worker_handler(
    tmp_path: Path,
    task_type: str,
):
    engine, _, config_id = seeded_database(tmp_path)
    with Session(engine) as db, pytest.raises(ValueError, match="任务类型无效"):
        enqueue_task(db, config_id, task_type, "2026-07-24")


def test_execution_task_can_use_decision_specific_idempotency_suffix(tmp_path: Path):
    engine, _, config_id = seeded_database(tmp_path)
    with Session(engine) as db:
        first = enqueue_task(
            db,
            config_id,
            "execute",
            "2026-07-27",
            payload={"decision_id": 41},
            idempotency_suffix="decision-41",
        )
        retry = enqueue_task(
            db,
            config_id,
            "execute",
            "2026-07-28",
            payload={"decision_id": 41},
            idempotency_suffix="decision-41",
        )

        assert first.id == retry.id
        assert first.idempotency_key.endswith(":execute:decision-41")


def test_claim_is_atomic_and_expired_lease_is_recoverable(tmp_path: Path):
    engine, _, config_id = seeded_database(tmp_path)
    factory = sessionmaker(bind=engine)
    current = datetime(2026, 7, 24, 16, 31, tzinfo=SHANGHAI)
    with factory() as db:
        task = enqueue_task(db, config_id, "signal", "2026-07-24")

    first = factory()
    second = factory()
    try:
        claimed = claim_pending_task(first, worker_id="worker-1", current=current)
        assert claimed is not None
        assert claim_pending_task(second, worker_id="worker-2", current=current) is None

        claimed.lease_until = current - timedelta(seconds=1)
        first.commit()
        reclaimed = claim_pending_task(second, worker_id="worker-2", current=current)

        assert reclaimed is not None
        assert reclaimed.id == task.id
        assert reclaimed.worker_id == "worker-2"
        assert reclaimed.attempts == 2
    finally:
        first.close()
        second.close()


def test_third_task_failure_pauses_only_its_strategy(tmp_path: Path):
    engine, _, config_id = seeded_database(tmp_path)
    current = datetime(2026, 7, 24, 16, 31, tzinfo=SHANGHAI)
    with Session(engine) as db:
        task = enqueue_task(db, config_id, "signal", "2026-07-24")
        other_config_id = db.scalar(
            select(StrategyRiskProfile.strategy_config_id).where(
                StrategyRiskProfile.strategy_config_id != config_id
            )
        )
        other_schedules = list(
            db.scalars(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id == other_config_id
                )
            )
        )
        for schedule in other_schedules:
            schedule.enabled = True
        db.add(
            NotificationChannel(
                type="email",
                name="策略告警",
                enabled=True,
                recipient="ops@example.com",
                secret_ref="SMTP_PASSWORD",
                event_types=[
                    "quant_strategy_task_failed",
                    "quant_strategy_auto_paused",
                ],
            )
        )
        db.commit()
        for attempt in range(3):
            claimed = claim_pending_task(db, worker_id="worker", current=current + timedelta(minutes=attempt))
            fail_task(db, claimed, RuntimeError("行情失败"), retryable=True)
            if attempt < 2:
                claimed.next_retry_at = current + timedelta(minutes=attempt, seconds=-1)
                db.commit()

        schedules = list(
            db.scalars(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id == config_id
                )
            )
        )
        db.expire_all()
        readiness = quant_strategy_readiness(db, config_id)

        assert task.status == "failed"
        assert all(not schedule.enabled for schedule in schedules)
        assert all(schedule.enabled for schedule in other_schedules)
        assert readiness["status"] == "FAILED"
        deliveries = list(db.scalars(select(NotificationDelivery)))
        assert {item.event_type for item in deliveries} == {
            "quant_strategy_task_failed",
            "quant_strategy_auto_paused",
        }


def test_strategy_cannot_be_ready_without_backtest_and_current_dry_run(tmp_path: Path):
    engine, _, config_id = seeded_database(tmp_path)
    with Session(engine) as db:
        readiness = quant_strategy_readiness(db, config_id)

        assert readiness["simulation_only"] is True
        assert readiness["status"] in {"DATA_PENDING", "BACKTEST_PENDING"}
        assert readiness["automation_ready"] is False
        assert "合格回测" in "，".join(readiness["reasons"])
        assert "风险公告数据源尚未就绪" in readiness["reasons"]


def test_readiness_rejects_stale_corporate_event_source(tmp_path: Path):
    engine, _, config_id = seeded_database(tmp_path)
    with Session(engine) as db:
        source = db.scalar(
            select(DataSourceState).where(
                DataSourceState.provider == "akshare_events"
            )
        )
        source.healthy = True
        source.last_checked_at = datetime.now(SHANGHAI) - timedelta(
            seconds=source.stale_after_seconds + 1
        )
        db.commit()

        readiness = quant_strategy_readiness(db, config_id)

        assert "风险公告数据已过期" in readiness["reasons"]


def test_benchmark_batch_failure_only_blocks_strategy_that_requires_it(
    tmp_path: Path,
):
    engine, _, _config_id = seeded_database(tmp_path)
    current_date = date(2026, 7, 24)
    with Session(engine) as db:
        for provider in (
            "quant_stock_daily",
            "quant_daily_metric",
            "quant_financial",
        ):
            db.add(
                DataSourceState(
                    provider=provider,
                    enabled=True,
                    healthy=True,
                    capabilities=[provider],
                    last_checked_at=datetime(2026, 7, 24, 16, 20, tzinfo=SHANGHAI),
                )
            )
        db.add(
            DataSourceState(
                provider="quant_benchmark_daily",
                enabled=True,
                healthy=False,
                capabilities=["quant_benchmark_daily"],
                last_checked_at=datetime(2026, 7, 24, 16, 20, tzinfo=SHANGHAI),
                last_error="沪深300同步失败",
            )
        )
        db.commit()

        relative_reasons = quant_dataset_state_reasons(
            db,
            "relative_strength_rotation",
            as_of=current_date,
        )
        reversal_reasons = quant_dataset_state_reasons(
            db,
            "short_term_reversal_t1",
            as_of=current_date,
        )

        assert not any("基准" in reason for reason in relative_reasons)
        assert any("基准" in reason for reason in reversal_reasons)


def test_readiness_requires_500_real_adjusted_trading_days(tmp_path: Path):
    engine, _, config_id = seeded_database(tmp_path)
    with Session(engine) as db:
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        start = date(2024, 1, 1)
        for offset in range(500):
            trade_date = start + timedelta(days=offset)
            db.add(
                MarketDailyBar(
                    stock_id=stock.id,
                    trade_date=trade_date.isoformat(),
                    open=10,
                    high=11,
                    low=9,
                    close=10,
                    adjusted_close=None,
                    adjustment_factor=None,
                    volume=100,
                    amount=200_000_000,
                    source="real-test",
                )
            )
        db.commit()

        readiness = quant_strategy_readiness(db, config_id)

        assert readiness["status"] == "DATA_PENDING"
        assert "复权" in "，".join(readiness["reasons"])

        for row in db.scalars(select(MarketDailyBar)):
            row.adjusted_close = row.close
            row.adjustment_factor = 1
        db.commit()
        readiness = quant_strategy_readiness(db, config_id)
        assert "真实复权日线不足500个交易日" not in readiness["reasons"]


def test_readiness_requires_current_stock_datasets_and_each_configured_etf(
    tmp_path: Path,
):
    engine, _, stock_config_id = seeded_database(tmp_path)
    with Session(engine) as db:
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        start = date(2024, 1, 1)
        for offset in range(500):
            trade_date = (start + timedelta(days=offset)).isoformat()
            db.add(
                MarketDailyBar(
                    stock_id=stock.id,
                    trade_date=trade_date,
                    open=10,
                    high=11,
                    low=9,
                    close=10,
                    adjusted_close=10,
                    adjustment_factor=1,
                    volume=100,
                    amount=200_000_000,
                    source="real-test",
                )
            )
        db.add(
            MarketDailyMetric(
                stock_id=stock.id,
                trade_date="2024-01-02",
                pe_ttm=10,
                pb=1,
                source="real-test",
            )
        )
        db.add(
            FinancialReportSnapshot(
                stock_id=stock.id,
                report_period="2023-12-31",
                announcement_date="2024-03-01",
                actual_announcement_date="2024-03-01",
                available_on="2024-03-04",
                roe=0.15,
                gross_margin=0.3,
                operating_cash_flow=10,
                total_assets=100,
                total_liabilities=30,
                source="real-test",
            )
        )
        etf_config = db.scalar(
            select(StrategyConfig)
            .join(StrategyDefinition)
            .where(StrategyDefinition.key == "risk_parity_overlay")
        )
        first_etf = Stock(
            code="510300",
            exchange="SSE",
            symbol="510300.SH",
            name="沪深300ETF",
            status="active",
            instrument_type="ETF",
        )
        db.add(first_etf)
        db.flush()
        for offset in range(500):
            trade_date = (start + timedelta(days=offset)).isoformat()
            db.add(
                MarketDailyBar(
                    stock_id=first_etf.id,
                    trade_date=trade_date,
                    open=4,
                    high=4.1,
                    low=3.9,
                    close=4,
                    adjusted_close=4,
                    adjustment_factor=1,
                    volume=100,
                    amount=200_000_000,
                    source="real-test",
                )
            )
        db.commit()

        stock_readiness = quant_strategy_readiness(db, stock_config_id)
        etf_readiness = quant_strategy_readiness(db, etf_config.id)

        assert "每日估值指标尚未更新到最新交易日" in stock_readiness["reasons"]
        assert "点时财务快照已过期" in stock_readiness["reasons"]
        assert "配置 ETF 逐只历史不足500个交易日" in etf_readiness["reasons"]


def test_system_emergency_stop_closes_quant_automation_readiness(tmp_path: Path):
    engine, _, config_id = seeded_database(tmp_path)
    with Session(engine) as db:
        system_risk = db.scalar(
            select(RiskSettings).where(RiskSettings.mode == "SIMULATION")
        )
        system_risk.emergency_stop_enabled = True
        db.commit()

        readiness = quant_strategy_readiness(db, config_id)

        assert readiness["automation_ready"] is False
        assert readiness["status"] == "PAUSED"
        assert "系统级紧急停止" in "，".join(readiness["reasons"])


def test_transient_retry_before_deadline_does_not_increment_strategy_errors(
    tmp_path: Path,
):
    engine, _, config_id = seeded_database(tmp_path)
    current = datetime(2026, 7, 27, 9, 35, tzinfo=SHANGHAI)
    with Session(engine) as db:
        task = enqueue_task(
            db,
            config_id,
            "execute",
            "2026-07-27",
            deadline_at=current.replace(hour=10, minute=0),
            max_attempts=100,
        )
        claimed = claim_pending_task(db, worker_id="worker", current=current)

        fail_task(
            db,
            claimed,
            RuntimeError("行情暂时不可用"),
            retryable=True,
            current=current,
        )

        risk = db.scalar(
            select(StrategyRiskProfile).where(
                StrategyRiskProfile.strategy_config_id == config_id
            )
        )
        assert task.status == "retry"
        assert risk.consecutive_errors == 0


def test_retry_past_deadline_fails_once_and_counts_strategy_error(tmp_path: Path):
    engine, _, config_id = seeded_database(tmp_path)
    deadline = datetime(2026, 7, 27, 10, 0, tzinfo=SHANGHAI)
    with Session(engine) as db:
        task = enqueue_task(
            db,
            config_id,
            "execute",
            "2026-07-27",
            deadline_at=deadline,
            max_attempts=100,
        )
        claimed = claim_pending_task(
            db,
            worker_id="worker",
            current=deadline - timedelta(seconds=10),
        )

        fail_task(
            db,
            claimed,
            RuntimeError("行情仍不可用"),
            retryable=True,
            current=deadline,
        )

        risk = db.scalar(
            select(StrategyRiskProfile).where(
                StrategyRiskProfile.strategy_config_id == config_id
            )
        )
        assert task.status == "failed"
        assert task.completed_at is not None
        assert risk.consecutive_errors == 1
        schedules = list(
            db.scalars(
                select(StrategySchedule).where(
                    StrategySchedule.strategy_config_id == config_id
                )
            )
        )
        assert all(schedule.enabled for schedule in schedules)


def test_claim_prioritizes_execution_and_uses_task_specific_lease(tmp_path: Path):
    engine, _, config_id = seeded_database(tmp_path)
    current = datetime(2026, 7, 27, 9, 35, tzinfo=SHANGHAI)
    with Session(engine) as db:
        backtest = enqueue_task(
            db,
            config_id,
            "backtest",
            "2026-07-24",
            payload={"start_date": "2023-01-01", "end_date": "2026-07-24"},
        )
        execute = enqueue_task(
            db,
            config_id,
            "execute",
            "2026-07-27",
            idempotency_suffix="priority-execution",
        )

        claimed = claim_pending_task(db, worker_id="worker", current=current)

        assert claimed.id == execute.id
        assert claimed.lease_until.replace(tzinfo=SHANGHAI) >= current + timedelta(minutes=1)
        execute.status = "completed"
        execute.lease_until = None
        db.commit()
        claimed_backtest = claim_pending_task(
            db,
            worker_id="worker",
            current=current,
        )
        assert claimed_backtest.id == backtest.id
        assert claimed_backtest.lease_until.replace(tzinfo=SHANGHAI) >= current + timedelta(hours=1)


def test_claim_can_isolate_one_strategy_lane_from_backtests_and_other_strategies(
    tmp_path: Path,
):
    engine, _, config_id = seeded_database(tmp_path)
    current = datetime(2026, 7, 27, 9, 35, tzinfo=SHANGHAI)
    with Session(engine) as db:
        other_config_id = db.scalar(
            select(StrategyConfig.id).where(StrategyConfig.id != config_id)
        )
        own_backtest = enqueue_task(
            db,
            config_id,
            "backtest",
            "2026-07-24",
            idempotency_suffix="lane-backtest",
        )
        own_signal = enqueue_task(
            db,
            config_id,
            "signal",
            "2026-07-24",
            idempotency_suffix="lane-signal",
        )
        other_execute = enqueue_task(
            db,
            other_config_id,
            "execute",
            "2026-07-27",
            idempotency_suffix="lane-execute",
        )

        claimed = claim_pending_task(
            db,
            worker_id="strategy-lane",
            current=current,
            strategy_config_id=config_id,
            task_types={"signal", "execute"},
        )

        assert claimed.id == own_signal.id
        assert own_backtest.status == "pending"
        assert other_execute.status == "pending"

        claimed_backtest = claim_pending_task(
            db,
            worker_id="backtest-lane",
            current=current,
            task_types={"backtest"},
        )
        assert claimed_backtest.id == own_backtest.id
