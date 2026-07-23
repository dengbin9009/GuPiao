from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import (
    Fill,
    Order,
    Position,
    ProbabilityCandidateDecision,
    ProbabilityModelArtifact,
    ProbabilityPortfolioRun,
    SimulationAccount,
    Stock,
    StrategyPositionLot,
)
from app.probability_portfolio.execution import (
    ScoredCandidate,
    execute_portfolio_entry,
    execute_portfolio_exit,
)
from app.probability_portfolio.runtime import seed_probability_portfolio_runtime
from app.services import seed_database


SHANGHAI = ZoneInfo("Asia/Shanghai")
CURRENT = datetime(2026, 7, 23, 14, 40, 10, tzinfo=SHANGHAI)


def setup_runtime(tmp_path: Path, *, dry_run: bool):
    engine = create_engine(f"sqlite:///{tmp_path / 'execution.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        config.parameters = {**config.parameters, "dry_run": dry_run}
        stocks = []
        for index in range(1, 5):
            stock = Stock(
                code=f"600{index:03d}",
                exchange="SSE",
                symbol=f"600{index:03d}.SH",
                name=f"概率候选{index}",
                status="active",
                last_price=10 + index,
                quote_updated_at=CURRENT - timedelta(seconds=5),
            )
            db.add(stock)
            db.flush()
            stocks.append(stock)
        if not dry_run:
            db.add(
                ProbabilityModelArtifact(
                    model_version="ready-test",
                    feature_version="1",
                    status="ready",
                    trained_through="2026-07-22",
                    training_sample_count=500,
                    calibration_sample_count=100,
                    calibration_start="2026-05-01",
                    calibration_end="2026-07-22",
                    brier_score=0.20,
                    coefficients={},
                    calibration_curve=[],
                    artifact_sha256="a" * 64,
                )
            )
        db.commit()
        return engine, config.id, config.simulation_account_id, [item.id for item in stocks]


def scored(stock_ids: list[int]) -> list[ScoredCandidate]:
    return [
        ScoredCandidate(
            stock_id=stock_id,
            symbol=f"600{index:03d}.SH",
            features={"volatility_20d": 0.08 + index / 100},
            raw_probability=0.66 - index / 100,
            calibrated_probability=0.66 - index / 100,
            expected_net_return=0.03 - index / 1000,
            volatility_20d=0.08 + index / 100,
        )
        for index, stock_id in enumerate(stock_ids, start=1)
    ]


def test_entry_blocks_without_ready_model_and_creates_no_orders(tmp_path):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=False)
    with Session(engine) as db:
        artifact = db.scalar(select(ProbabilityModelArtifact))
        artifact.status = "rejected"
        db.commit()
        config = db.get(__import__("app.models", fromlist=["StrategyConfig"]).StrategyConfig, config_id)

        run = execute_portfolio_entry(
            db,
            config,
            current=CURRENT,
            scored_candidates=scored(stock_ids),
        )

        assert run.summary["accepted"] == 0
        assert "模型" in run.summary["reason"]
        assert db.scalar(select(func.count(Order.id)).where(Order.account_id == account_id)) == 0


def test_dry_run_records_non_equal_allocations_without_orders(tmp_path):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=True)
    with Session(engine) as db:
        from app.models import StrategyConfig

        config = db.get(StrategyConfig, config_id)
        run = execute_portfolio_entry(
            db,
            config,
            current=CURRENT,
            scored_candidates=scored(stock_ids[:3]),
        )
        decisions = list(
            db.scalars(
                select(ProbabilityCandidateDecision).order_by(
                    ProbabilityCandidateDecision.rank
                )
            )
        )

        assert run.summary["dry_run"] is True
        assert run.summary["selected"] == 3
        assert run.summary["order_ids"] == []
        assert db.scalar(select(func.count(Order.id)).where(Order.account_id == account_id)) == 0
        assert len({round(item.target_weight, 8) for item in decisions}) > 1


def test_entry_trades_up_to_available_candidates_in_dedicated_account(tmp_path):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=False)
    with Session(engine) as db:
        from app.models import StrategyConfig

        config = db.get(StrategyConfig, config_id)
        default_account = db.scalar(
            select(SimulationAccount).where(SimulationAccount.id != account_id).order_by(
                SimulationAccount.id
            )
        )
        default_cash = default_account.cash_balance

        run = execute_portfolio_entry(
            db,
            config,
            current=CURRENT,
            scored_candidates=scored(stock_ids[:3]),
        )
        second = execute_portfolio_entry(
            db,
            config,
            current=CURRENT,
            scored_candidates=scored(stock_ids[:3]),
        )
        orders = list(db.scalars(select(Order).where(Order.account_id == account_id)))
        fills = list(db.scalars(select(Fill).where(Fill.account_id == account_id)))
        lots = list(
            db.scalars(select(StrategyPositionLot).where(StrategyPositionLot.account_id == account_id))
        )
        positions = list(
            db.scalars(
                select(Position).where(
                    Position.account_id == account_id,
                    Position.quantity > 0,
                )
            )
        )
        account = db.get(SimulationAccount, account_id)

        assert run.summary["accepted"] == 3
        assert second.id == run.id
        assert len(orders) == len(fills) == len(lots) == len(positions) == 3
        assert all(order.side == "buy" and order.mode == "SIMULATION" for order in orders)
        assert all(lot.planned_exit_at.hour == 10 and lot.planned_exit_at.minute == 30 for lot in lots)
        assert all(lot.available_on == "2026-07-24" for lot in lots)
        assert account.cash_balance < 2_000_000
        assert account.total_asset == account.cash_balance + sum(item.market_value for item in positions)
        assert default_account.cash_balance == default_cash
        assert db.scalar(select(func.count(ProbabilityPortfolioRun.id))) == 1


def test_one_unaffordable_candidate_is_skipped_without_blocking_others(tmp_path):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=False)
    with Session(engine) as db:
        from app.models import StrategyConfig

        expensive = db.get(Stock, stock_ids[0])
        expensive.last_price = 1_000_000
        config = db.get(StrategyConfig, config_id)

        run = execute_portfolio_entry(
            db,
            config,
            current=CURRENT,
            scored_candidates=scored(stock_ids[:2]),
        )

        assert run.summary["accepted"] == 1
        assert any(
            item["symbol"] == expensive.symbol and "一手" in item["reason"]
            for item in run.summary["skipped"]
        )
        assert db.scalar(select(func.count(Order.id)).where(Order.account_id == account_id)) == 1


def _open_lots(engine, config_id, stock_ids):
    with Session(engine) as db:
        from app.models import StrategyConfig

        config = db.get(StrategyConfig, config_id)
        execute_portfolio_entry(
            db,
            config,
            current=CURRENT,
            scored_candidates=scored(stock_ids[:2]),
        )


def test_exit_sells_owned_lots_at_1030_and_is_idempotent(tmp_path):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=False)
    _open_lots(engine, config_id, stock_ids)
    exit_at = datetime(2026, 7, 24, 10, 30, 5, tzinfo=SHANGHAI)
    with Session(engine) as db:
        from app.models import StrategyConfig

        for stock_id in stock_ids[:2]:
            stock = db.get(Stock, stock_id)
            stock.last_price += 0.2
            stock.quote_updated_at = exit_at - timedelta(seconds=5)
        config = db.get(StrategyConfig, config_id)

        run = execute_portfolio_exit(db, config, current=exit_at)
        second = execute_portfolio_exit(db, config, current=exit_at)
        lots = list(db.scalars(select(StrategyPositionLot).where(StrategyPositionLot.account_id == account_id)))
        positions = list(db.scalars(select(Position).where(Position.account_id == account_id)))
        sells = list(
            db.scalars(
                select(Order).where(Order.account_id == account_id, Order.side == "sell")
            )
        )

        assert run.summary["accepted"] == 2
        assert second.id == run.id
        assert len(sells) == 2
        assert all(lot.status == "closed" and lot.remaining_quantity == 0 for lot in lots)
        assert all(position.quantity == 0 and position.market_value == 0 for position in positions)


def test_exit_retries_stale_quotes_until_1045_without_fake_fill(tmp_path):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=False)
    _open_lots(engine, config_id, stock_ids)
    exit_at = datetime(2026, 7, 24, 10, 31, tzinfo=SHANGHAI)
    with Session(engine) as db:
        from app.models import StrategyConfig

        config = db.get(StrategyConfig, config_id)
        run = execute_portfolio_exit(db, config, current=exit_at)
        lots = list(db.scalars(select(StrategyPositionLot).where(StrategyPositionLot.account_id == account_id)))
        sells = db.scalar(
            select(func.count(Order.id)).where(Order.account_id == account_id, Order.side == "sell")
        )

        assert run.summary["accepted"] == 0
        assert run.summary["retryable"] is True
        assert "行情" in run.summary["reason"]
        assert sells == 0
        assert all(lot.status == "open" and lot.remaining_quantity > 0 for lot in lots)


def test_exit_after_1045_keeps_lot_for_next_trading_day(tmp_path):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=False)
    _open_lots(engine, config_id, stock_ids)
    after_deadline = datetime(2026, 7, 24, 10, 46, tzinfo=SHANGHAI)
    with Session(engine) as db:
        from app.models import StrategyConfig

        config = db.get(StrategyConfig, config_id)
        run = execute_portfolio_exit(db, config, current=after_deadline)
        lots = list(db.scalars(select(StrategyPositionLot).where(StrategyPositionLot.account_id == account_id)))

        assert run.summary["accepted"] == 0
        assert run.summary["retryable"] is False
        assert "10:45" in run.summary["reason"]
        assert all(lot.status == "open" for lot in lots)

    next_day = datetime(2026, 7, 27, 10, 30, 5, tzinfo=SHANGHAI)
    with Session(engine) as db:
        from app.models import StrategyConfig

        for stock_id in stock_ids[:2]:
            stock = db.get(Stock, stock_id)
            stock.quote_updated_at = next_day - timedelta(seconds=5)
        config = db.get(StrategyConfig, config_id)
        recovered = execute_portfolio_exit(db, config, current=next_day)

        assert recovered.summary["accepted"] == 2
