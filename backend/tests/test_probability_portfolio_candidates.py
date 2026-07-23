from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import (
    DataSourceState,
    MarketDailyBar,
    ProbabilityModelArtifact,
    Stock,
)
from app.probability_portfolio.candidates import build_scored_candidates
from app.probability_portfolio.runtime import seed_probability_portfolio_runtime
from app.services import seed_database


SHANGHAI = ZoneInfo("Asia/Shanghai")
CURRENT = datetime(2026, 7, 23, 14, 40, tzinfo=SHANGHAI)
FEATURE_NAMES = (
    "intraday_return",
    "turnover_amount_log",
    "turnover_rate",
    "vwap_distance",
    "tail_30m_return",
    "close_location",
    "ma5_distance",
    "ma20_distance",
    "momentum_5d",
    "momentum_20d",
    "volatility_20d",
    "average_amount_20d_log",
    "benchmark_ma5_distance",
    "relative_strength",
    "market_breadth",
)


def add_bars(db, stock_id: int, *, start_price: float = 10):
    for index in range(20):
        day = CURRENT.date() - timedelta(days=20 - index)
        price = start_price + index * 0.05
        db.add(
            MarketDailyBar(
                stock_id=stock_id,
                trade_date=day.isoformat(),
                open=price - 0.03,
                high=price + 0.08,
                low=price - 0.08,
                close=price,
                volume=10_000_000,
                amount=200_000_000,
                source="test",
                captured_at=CURRENT - timedelta(days=1),
            )
        )


def add_ready_artifact(db):
    db.add(
        ProbabilityModelArtifact(
            model_version="candidate-test",
            feature_version="1",
            status="ready",
            trained_through="2026-07-22",
            training_sample_count=500,
            calibration_sample_count=100,
            calibration_start="2026-05-01",
            calibration_end="2026-07-22",
            brier_score=0.20,
            coefficients={
                "feature_names": list(FEATURE_NAMES),
                "model": {
                    "intercept": 1.0,
                    "weights": [0.0] * len(FEATURE_NAMES),
                    "means": [0.0] * len(FEATURE_NAMES),
                    "scales": [1.0] * len(FEATURE_NAMES),
                },
                "average_win": 0.03,
                "average_loss": -0.01,
            },
            calibration_curve=[
                {"raw": 0.0, "calibrated": 0.60},
                {"raw": 1.0, "calibrated": 0.70},
            ],
            artifact_sha256="b" * 64,
        )
    )


def test_candidate_builder_scores_complete_rows_and_records_rejections(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'candidates.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        config.parameters = {**config.parameters, "prefilter_size": 20}
        source = db.scalar(
            select(DataSourceState).where(DataSourceState.provider == "mootdx")
        )
        source.healthy = True
        source.last_checked_at = CURRENT
        event_source = db.scalar(
            select(DataSourceState).where(
                DataSourceState.provider == "akshare_events"
            )
        )
        event_source.healthy = True
        event_source.last_checked_at = CURRENT

        benchmark = Stock(
            code="000300",
            exchange="SSE",
            symbol="000300.SH",
            name="沪深300",
            status="active",
        )
        db.add(benchmark)
        db.flush()
        add_bars(db, benchmark.id, start_price=20)

        valid = Stock(
            code="600901",
            exchange="SSE",
            symbol="600901.SH",
            name="完整候选",
            status="active",
            listing_date="2020-01-01",
            last_price=11.2,
            change_pct=3,
            turnover_amount=300_000_000,
            turnover_rate=0.02,
            open_price=10.95,
            high_price=11.3,
            low_price=10.9,
            volume=20_000_000,
            vwap=11.05,
            tail_30m_return=0.008,
            limit_up_price=12,
            limit_down_price=9.8,
            quote_source="mootdx",
            quote_updated_at=CURRENT - timedelta(seconds=5),
            factor_updated_at=CURRENT - timedelta(seconds=5),
        )
        invalid = Stock(
            code="600902",
            exchange="SSE",
            symbol="600902.SH",
            name="缺失候选",
            status="active",
            last_price=10,
            change_pct=3,
            turnover_amount=250_000_000,
            quote_source="mootdx",
            quote_updated_at=CURRENT - timedelta(seconds=5),
        )
        db.add_all([valid, invalid])
        db.flush()
        add_bars(db, valid.id)
        add_bars(db, invalid.id)
        add_ready_artifact(db)
        db.commit()

        result = build_scored_candidates(db, config, current=CURRENT)

        assert [item.symbol for item in result.scored] == ["600901.SH"]
        assert result.scored[0].calibrated_probability >= 0.60
        assert result.scored[0].expected_net_return > 0
        rejected = {item.symbol: item.reasons for item in result.rejected}
        assert "600902.SH" in rejected
        assert "缺少真实换手率" in rejected["600902.SH"]
        assert result.model_artifact_id is not None


def test_candidate_builder_reports_missing_ready_model(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'no-model.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)

        result = build_scored_candidates(db, config, current=CURRENT)

        assert result.scored == []
        assert result.model_artifact_id is None
        assert "概率模型尚未就绪" in result.reasons
        assert result.rejected
        assert all(
            "概率模型尚未就绪" in item.reasons or len(item.reasons) > 1
            for item in result.rejected
        )


def test_candidate_builder_fails_closed_when_event_source_is_stale(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stale-events.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        add_ready_artifact(db)
        event_source = db.scalar(
            select(DataSourceState).where(
                DataSourceState.provider == "akshare_events"
            )
        )
        event_source.healthy = True
        event_source.last_checked_at = CURRENT - timedelta(hours=1)
        db.commit()

        result = build_scored_candidates(db, config, current=CURRENT)

        assert result.scored == []
        assert "公司事件数据未就绪或已过期" in result.reasons
        assert result.rejected
        assert all(
            "公司事件数据未就绪或已过期" in item.reasons
            for item in result.rejected
        )


def test_candidate_builder_keeps_1800_second_event_hard_cap(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'event-hard-cap.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        config.parameters = {
            **config.parameters,
            "event_max_age_seconds": 999_999,
        }
        add_ready_artifact(db)
        event_source = db.scalar(
            select(DataSourceState).where(
                DataSourceState.provider == "akshare_events"
            )
        )
        event_source.healthy = True
        event_source.last_checked_at = CURRENT - timedelta(seconds=1801)
        db.commit()

        result = build_scored_candidates(db, config, current=CURRENT)

        assert "公司事件数据未就绪或已过期" in result.reasons


def test_candidate_builder_never_scans_more_than_top_100_by_turnover(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'top-100.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        config.parameters = {**config.parameters, "prefilter_size": 999}
        for index in range(101):
            db.add(
                Stock(
                    code=f"60{index:04d}",
                    exchange="SSE",
                    symbol=f"60{index:04d}.SH",
                    name=f"预筛上限{index}",
                    status="active",
                    turnover_amount=1_000_000_000 - index,
                )
            )
        db.commit()

        result = build_scored_candidates(db, config, current=CURRENT)

        assert len(result.scored) + len(result.rejected) == 100
