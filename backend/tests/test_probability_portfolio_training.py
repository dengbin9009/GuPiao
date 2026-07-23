from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.config import Settings
from app.models import (
    Order,
    ProbabilityCandidateDecision,
    ProbabilityModelArtifact,
    ProbabilityTrainingSample,
    SimulationAccount,
    Stock,
)
from app.probability_portfolio.execution import RejectedCandidate
from app.probability_portfolio.features import FEATURE_NAMES
from app.probability_portfolio.model import build_window_label
from app.probability_portfolio.observation import (
    finalize_probability_training_samples,
    pending_observation_symbols,
    record_probability_observation,
)
from app.probability_portfolio.runtime import seed_probability_portfolio_runtime
from app.probability_portfolio.training import train_and_store_probability_model
from app.services import seed_database
from scripts.train_probability_portfolio import build_parser


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _features(index: int) -> dict[str, float]:
    return {
        name: (index % (offset + 7) - 3) / 100
        for offset, name in enumerate(FEATURE_NAMES)
    }


def _sample(stock_id: int, index: int) -> ProbabilityTrainingSample:
    entry_at = datetime(2025, 1, 2, 14, 40, tzinfo=SHANGHAI) + timedelta(
        days=index
    )
    profitable = bool(index % 2)
    return ProbabilityTrainingSample(
        stock_id=stock_id,
        entry_at=entry_at,
        exit_at=entry_at + timedelta(days=1, hours=-4, minutes=-10),
        feature_version="1",
        features=_features(index),
        net_return=0.012 if profitable else -0.008,
        profitable=profitable,
        source_sha256=f"{index:064x}"[-64:],
    )


def test_training_persists_an_idempotent_ready_artifact_without_future_exits(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'training.db'}")
    Base.metadata.create_all(engine)
    through = date(2025, 5, 15)
    with Session(engine) as db:
        stock = Stock(
            code="600901",
            exchange="SSE",
            symbol="600901.SH",
            name="训练样本",
            status="active",
        )
        db.add(stock)
        db.flush()
        db.add_all(_sample(stock.id, index) for index in range(120))
        future = _sample(stock.id, 121)
        future.entry_at = datetime(2025, 5, 15, 14, 40, tzinfo=SHANGHAI)
        future.exit_at = datetime(2025, 5, 16, 10, 30, tzinfo=SHANGHAI)
        db.add(future)
        db.commit()

        first = train_and_store_probability_model(
            db,
            through=through,
            feature_version="1",
            min_training_samples=80,
            min_calibration_samples=20,
            max_brier_score=1.0,
        )
        second = train_and_store_probability_model(
            db,
            through=through,
            feature_version="1",
            min_training_samples=80,
            min_calibration_samples=20,
            max_brier_score=1.0,
        )

        assert first.id == second.id
        assert first.status == "ready"
        assert first.training_sample_count == 96
        assert first.calibration_sample_count == 24
        assert first.trained_through == through.isoformat()
        assert first.coefficients["feature_names"] == list(FEATURE_NAMES)
        assert set(first.coefficients["model"]) == {
            "intercept",
            "weights",
            "means",
            "scales",
        }
        assert first.artifact_sha256
        assert db.scalar(select(func.count(ProbabilityModelArtifact.id))) == 1


def test_training_saves_rejected_artifact_when_samples_are_insufficient(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'insufficient.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        stock = Stock(
            code="600902",
            exchange="SSE",
            symbol="600902.SH",
            name="样本不足",
            status="active",
        )
        db.add(stock)
        db.flush()
        db.add_all(_sample(stock.id, index) for index in range(20))
        db.commit()

        artifact = train_and_store_probability_model(
            db,
            through=date(2025, 12, 31),
            feature_version="1",
            min_training_samples=80,
            min_calibration_samples=20,
            max_brier_score=0.25,
        )

        assert artifact.status == "rejected"
        assert "training_samples" in artifact.error_message
        assert "calibration_samples" in artifact.error_message
        assert artifact.training_sample_count == 16
        assert artifact.calibration_sample_count == 4


def test_training_cli_requires_an_explicit_cutoff_and_never_enables_schedules():
    parser = build_parser()
    args = parser.parse_args(["--through", "2026-07-22"])

    assert args.through == "2026-07-22"
    assert not hasattr(args, "enable")


def test_observation_and_next_day_label_build_real_training_sample_without_orders(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'observation.db'}")
    Base.metadata.create_all(engine)
    entry_at = datetime(2026, 7, 23, 14, 40, 5, tzinfo=SHANGHAI)
    exit_at = datetime(2026, 7, 24, 10, 30, 5, tzinfo=SHANGHAI)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        stock.last_price = 10.0
        stock.quote_updated_at = entry_at
        result = record_probability_observation(
            db,
            config,
            current=entry_at,
            scored_candidates=[],
            rejected_candidates=[
                RejectedCandidate(
                    stock_id=stock.id,
                    symbol=stock.symbol,
                    reasons=("概率模型尚未就绪",),
                    features={name: 0.01 for name in FEATURE_NAMES},
                )
            ],
            candidate_reasons=("概率模型尚未就绪",),
        )

        assert result.summary["accepted"] == 0
        assert result.summary["training_exit_date"] == "2026-07-24"
        assert db.scalar(select(func.count(Order.id))) == 0

        stock.last_price = 10.2
        stock.quote_updated_at = exit_at - timedelta(seconds=5)
        db.commit()
        first = finalize_probability_training_samples(
            db,
            config,
            current=exit_at,
        )
        second = finalize_probability_training_samples(
            db,
            config,
            current=exit_at,
        )
        sample_row = db.scalar(select(ProbabilityTrainingSample))

        assert first == {"created": 1, "skipped": 0, "errors": 0}
        assert second == {"created": 0, "skipped": 1, "errors": 0}
        assert sample_row.entry_at.hour == 14 and sample_row.entry_at.minute == 40
        assert sample_row.exit_at.hour == 10 and sample_row.exit_at.minute == 30
        assert sample_row.profitable is True
        assert sample_row.net_return > 0
        assert set(sample_row.features) == set(FEATURE_NAMES)


def test_training_label_uses_frozen_entry_costs_and_quantity(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'frozen-label.db'}")
    Base.metadata.create_all(engine)
    entry_at = datetime(2026, 7, 23, 14, 40, tzinfo=SHANGHAI)
    exit_at = datetime(2026, 7, 24, 10, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        account = db.get(
            SimulationAccount,
            config.simulation_account_id,
        )
        stock.last_price = 10
        stock.quote_updated_at = entry_at
        original = {
            "commission_rate": account.commission_rate,
            "min_commission": account.min_commission,
            "stamp_tax_rate": account.stamp_tax_rate,
            "transfer_fee_rate": account.transfer_fee_rate,
            "slippage_bps": account.slippage_bps,
        }
        record_probability_observation(
            db,
            config,
            current=entry_at,
            scored_candidates=[],
            rejected_candidates=[
                RejectedCandidate(
                    stock.id,
                    stock.symbol,
                    ("概率模型尚未就绪",),
                    {name: 0.01 for name in FEATURE_NAMES},
                )
            ],
            candidate_reasons=("概率模型尚未就绪",),
        )

        account.total_asset = 1_000_000
        account.commission_rate = 0.05
        account.min_commission = 1_000
        account.stamp_tax_rate = 0.05
        account.transfer_fee_rate = 0.01
        account.slippage_bps = 500
        stock.last_price = 10.2
        stock.quote_updated_at = exit_at - timedelta(seconds=5)
        db.commit()

        result = finalize_probability_training_samples(db, config, current=exit_at)
        sample_row = db.scalar(select(ProbabilityTrainingSample))
        decision = db.scalar(select(ProbabilityCandidateDecision))
        expected = build_window_label(
            entry_at=entry_at,
            exit_at=exit_at,
            entry_price=10,
            exit_price=10.2,
            quantity=3900,
            **original,
        )

        assert result == {"created": 1, "skipped": 0, "errors": 0}
        assert decision.features["_label_quantity"] == 3900
        assert sample_row.net_return == pytest.approx(expected.net_return)


def test_training_label_refuses_stale_exit_quote(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stale-label.db'}")
    Base.metadata.create_all(engine)
    entry_at = datetime(2026, 7, 23, 14, 40, tzinfo=SHANGHAI)
    exit_at = datetime(2026, 7, 24, 10, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        stock.last_price = 10
        record_probability_observation(
            db,
            config,
            current=entry_at,
            scored_candidates=[],
            rejected_candidates=[
                RejectedCandidate(
                    stock.id,
                    stock.symbol,
                    ("概率模型尚未就绪",),
                    {name: 0.01 for name in FEATURE_NAMES},
                )
            ],
            candidate_reasons=("概率模型尚未就绪",),
        )
        stock.last_price = 10.2
        stock.quote_updated_at = exit_at - timedelta(minutes=2)
        db.commit()

        result = finalize_probability_training_samples(
            db,
            config,
            current=exit_at,
        )

        assert result == {"created": 0, "skipped": 0, "errors": 1}
        assert db.scalar(select(func.count(ProbabilityTrainingSample.id))) == 0


def test_observation_excludes_complete_features_when_event_data_is_stale(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stale-event-observation.db'}")
    Base.metadata.create_all(engine)
    entry_at = datetime(2026, 7, 23, 14, 40, tzinfo=SHANGHAI)
    exit_at = datetime(2026, 7, 24, 10, 30, tzinfo=SHANGHAI)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        stock.last_price = 10
        stock.quote_updated_at = entry_at
        record_probability_observation(
            db,
            config,
            current=entry_at,
            scored_candidates=[],
            rejected_candidates=[
                RejectedCandidate(
                    stock.id,
                    stock.symbol,
                    ("概率模型尚未就绪", "公司事件数据未就绪或已过期"),
                    {name: 0.01 for name in FEATURE_NAMES},
                )
            ],
            candidate_reasons=("概率模型尚未就绪", "公司事件数据未就绪或已过期"),
        )

        symbols = pending_observation_symbols(db, config, current=exit_at)

        assert symbols == []


def test_observation_can_roll_to_first_actual_trading_day_only(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'holiday-label.db'}")
    Base.metadata.create_all(engine)
    entry_at = datetime(2026, 7, 23, 14, 40, tzinfo=SHANGHAI)
    delayed_exit = datetime(2026, 7, 27, 10, 30, tzinfo=SHANGHAI)

    class HolidayCalendar:
        def trading_days(self, *, start, end):
            assert start.isoformat() == "2026-07-24"
            assert end.isoformat() == "2026-07-27"
            return ["2026-07-27"]

    class ClosedCalendar:
        def trading_days(self, *, start, end):
            assert start.isoformat() == end.isoformat() == "2026-07-24"
            return []

    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        stock.last_price = 10
        stock.quote_updated_at = entry_at
        record_probability_observation(
            db,
            config,
            current=entry_at,
            scored_candidates=[],
            rejected_candidates=[
                RejectedCandidate(
                    stock.id,
                    stock.symbol,
                    ("概率模型尚未就绪",),
                    {name: 0.01 for name in FEATURE_NAMES},
                )
            ],
            candidate_reasons=("概率模型尚未就绪",),
        )
        stock.last_price = 10.2
        stock.quote_updated_at = delayed_exit - timedelta(seconds=5)
        db.commit()

        assert pending_observation_symbols(
            db,
            config,
            current=datetime(2026, 7, 24, 10, 30, tzinfo=SHANGHAI),
            calendar=ClosedCalendar(),
        ) == []
        symbols = pending_observation_symbols(
            db,
            config,
            current=delayed_exit,
            calendar=HolidayCalendar(),
        )
        result = finalize_probability_training_samples(
            db,
            config,
            current=delayed_exit,
            calendar=HolidayCalendar(),
        )
        sample = db.scalar(select(ProbabilityTrainingSample))

        assert symbols == [stock.symbol]
        assert result == {"created": 1, "skipped": 0, "errors": 0}
        assert sample.exit_at.date() == delayed_exit.date()


def test_pending_observation_propagates_calendar_failure_for_retry(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'calendar-failure.db'}")
    Base.metadata.create_all(engine)
    entry_at = datetime(2026, 7, 23, 14, 40, tzinfo=SHANGHAI)

    class BrokenCalendar:
        def trading_days(self, *, start, end):
            raise RuntimeError("交易日历暂不可用")

    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        stock.last_price = 10
        stock.quote_updated_at = entry_at
        record_probability_observation(
            db,
            config,
            current=entry_at,
            scored_candidates=[],
            rejected_candidates=[
                RejectedCandidate(
                    stock.id,
                    stock.symbol,
                    ("概率模型尚未就绪",),
                    {name: 0.01 for name in FEATURE_NAMES},
                )
            ],
            candidate_reasons=("概率模型尚未就绪",),
        )

        with pytest.raises(RuntimeError, match="交易日历"):
            pending_observation_symbols(
                db,
                config,
                current=datetime(2026, 7, 24, 10, 30, tzinfo=SHANGHAI),
                calendar=BrokenCalendar(),
            )
