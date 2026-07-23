from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import (
    Fill,
    AccountSnapshot,
    Order,
    Position,
    ProbabilityCandidateDecision,
    ProbabilityModelArtifact,
    ProbabilityPortfolioRun,
    SimulationAccount,
    Stock,
    StrategyConfig,
    StrategyPositionLot,
    StrategyRun,
    StrategySchedule,
)
from app.probability_portfolio.execution import (
    RejectedCandidate,
    ScoredCandidate,
    execute_portfolio_entry,
    execute_portfolio_exit,
)
from app.probability_portfolio.features import FEATURE_NAMES
from app.probability_portfolio.readiness import configuration_fingerprint
from app.probability_portfolio.runtime import seed_probability_portfolio_runtime
from app.services import seed_database


SHANGHAI = ZoneInfo("Asia/Shanghai")
CURRENT = datetime(2026, 7, 23, 14, 40, 10, tzinfo=SHANGHAI)


def setup_runtime(tmp_path: Path, *, dry_run: bool, validated: bool = True):
    engine = create_engine(f"sqlite:///{tmp_path / 'execution.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        settings = Settings(database_url=str(engine.url))
        seed_database(db, settings)
        config = seed_probability_portfolio_runtime(db, settings)
        config.parameters = {**config.parameters, "dry_run": dry_run}
        if not dry_run:
            schedules = list(
                db.scalars(
                    select(StrategySchedule).where(
                        StrategySchedule.strategy_config_id == config.id,
                    )
                )
            )
            for schedule in schedules:
                schedule.enabled = True
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
            artifact = ProbabilityModelArtifact(
                model_version="ready-test",
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
                        "intercept": 0.5,
                        "weights": [0.0] * len(FEATURE_NAMES),
                        "means": [0.0] * len(FEATURE_NAMES),
                        "scales": [1.0] * len(FEATURE_NAMES),
                    },
                    "average_win": 0.02,
                    "average_loss": -0.01,
                },
                calibration_curve=[
                    {"raw": 0.0, "calibrated": 0.55},
                    {"raw": 1.0, "calibrated": 0.70},
                ],
                artifact_sha256="a" * 64,
            )
            db.add(artifact)
            db.flush()
            if validated:
                validation_run = StrategyRun(
                    strategy_config_id=config.id,
                    mode="SIMULATION",
                    status="completed",
                    started_at=CURRENT,
                    finished_at=CURRENT,
                    summary={"data_ready": True},
                )
                db.add(validation_run)
                db.flush()
                db.add(
                    ProbabilityPortfolioRun(
                        strategy_run_id=validation_run.id,
                        strategy_config_id=config.id,
                        simulation_account_id=config.simulation_account_id,
                        trading_date=CURRENT.date().isoformat(),
                        trigger_type="portfolio_dry_setup",
                        status="completed",
                        dry_run=True,
                        model_artifact_id=artifact.id,
                        config_fingerprint=configuration_fingerprint(
                            config.parameters,
                            simulation_account_id=config.simulation_account_id,
                        ),
                        completed_at=CURRENT,
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


def test_entry_blocks_if_exit_schedule_is_disabled(tmp_path):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=False)
    with Session(engine) as db:
        from app.models import StrategyConfig

        config = db.get(StrategyConfig, config_id)
        exit_schedule = db.scalar(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id,
                StrategySchedule.trigger_type == "portfolio_exit",
            )
        )
        exit_schedule.enabled = False
        db.commit()

        run = execute_portfolio_entry(
            db,
            config,
            current=CURRENT,
            scored_candidates=scored(stock_ids[:1]),
        )

        assert run.summary["accepted"] == 0
        assert "退出计划" in run.summary["reason"]
        assert db.scalar(
            select(func.count(Order.id)).where(Order.account_id == account_id)
        ) == 0


def test_entry_blocks_if_entry_schedule_is_disabled(tmp_path):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=False)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_id)
        entry_schedule = db.scalar(
            select(StrategySchedule).where(
                StrategySchedule.strategy_config_id == config.id,
                StrategySchedule.trigger_type == "portfolio_entry",
            )
        )
        entry_schedule.enabled = False
        db.commit()

        run = execute_portfolio_entry(
            db,
            config,
            current=CURRENT,
            scored_candidates=scored(stock_ids[:1]),
        )

        assert run.summary["accepted"] == 0
        assert "入场计划" in run.summary["reason"]
        assert db.scalar(
            select(func.count(Order.id)).where(Order.account_id == account_id)
        ) == 0


def test_entry_blocks_if_simulation_account_is_shared(tmp_path):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=False)
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_id)
        db.add(
            StrategyConfig(
                strategy_definition_id=config.strategy_definition_id,
                name="违规共享账户配置",
                mode="SIMULATION",
                parameters={},
                simulation_account_id=account_id,
            )
        )
        db.commit()

        run = execute_portfolio_entry(
            db,
            config,
            current=CURRENT,
            scored_candidates=scored(stock_ids[:1]),
        )

        assert run.summary["accepted"] == 0
        assert "独立模拟账户" in run.summary["reason"]
        assert db.scalar(
            select(func.count(Order.id)).where(Order.account_id == account_id)
        ) == 0


def test_entry_blocks_when_database_plan_bypasses_required_dry_run(tmp_path):
    engine, config_id, account_id, stock_ids = setup_runtime(
        tmp_path,
        dry_run=False,
        validated=False,
    )
    with Session(engine) as db:
        config = db.get(StrategyConfig, config_id)

        run = execute_portfolio_entry(
            db,
            config,
            current=CURRENT,
            scored_candidates=scored(stock_ids[:1]),
        )

        assert run.summary["accepted"] == 0
        assert "演练" in run.summary["reason"]
        assert db.scalar(
            select(func.count(Order.id)).where(Order.account_id == account_id)
        ) == 0


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


def test_dry_run_persists_data_quality_rejections(tmp_path):
    engine, config_id, _, stock_ids = setup_runtime(tmp_path, dry_run=True)
    with Session(engine) as db:
        from app.models import StrategyConfig

        config = db.get(StrategyConfig, config_id)
        execute_portfolio_entry(
            db,
            config,
            current=CURRENT,
            scored_candidates=scored(stock_ids[:1]),
            rejected_candidates=[
                RejectedCandidate(
                    stock_id=stock_ids[1],
                    symbol="600002.SH",
                    reasons=("缺少真实日内VWAP", "行情已过期"),
                )
            ],
        )
        rejected = db.scalar(
            select(ProbabilityCandidateDecision).where(
                ProbabilityCandidateDecision.stock_id == stock_ids[1]
            )
        )

        assert rejected.status == "rejected"
        assert rejected.features == {}
        assert rejected.rejection_reasons == ["缺少真实日内VWAP", "行情已过期"]


def test_entry_checks_daily_loss_before_creating_pretrade_snapshot(
    tmp_path,
    monkeypatch,
):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=False)
    with Session(engine) as db:
        from app import simulation_accounts
        from app.models import StrategyConfig

        db.execute(
            delete(AccountSnapshot).where(
                AccountSnapshot.mode == "SIMULATION",
                AccountSnapshot.account_id == account_id,
            )
        )
        account = db.get(SimulationAccount, account_id)
        account.cash_balance = 1_960_000
        account.available_cash = 1_960_000
        account.total_asset = 1_960_000
        db.commit()
        current = datetime.now(SHANGHAI) + timedelta(minutes=1)

        def flushed_daily_pnl(db, account, *, current):
            db.flush()
            return simulation_accounts.daily_pnl_pct(db, account, current=current)

        monkeypatch.setattr(
            "app.probability_portfolio.execution.daily_pnl_pct",
            flushed_daily_pnl,
        )
        config = db.get(StrategyConfig, config_id)
        run = execute_portfolio_entry(
            db,
            config,
            current=current,
            scored_candidates=scored(stock_ids[:1]),
        )

        assert run.summary["accepted"] == 0
        assert "日亏损" in run.summary["reason"]
        assert db.scalar(
            select(func.count(Order.id)).where(Order.account_id == account_id)
        ) == 0


def test_entry_keeps_10_percent_daily_loss_hard_cap(tmp_path, monkeypatch):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=False)
    with Session(engine) as db:
        from app.models import StrategyConfig

        config = db.get(StrategyConfig, config_id)
        config.parameters = {
            **config.parameters,
            "daily_loss_limit_pct": 0.99,
        }
        validation = db.scalar(
            select(ProbabilityPortfolioRun).where(
                ProbabilityPortfolioRun.trigger_type == "portfolio_dry_setup"
            )
        )
        validation.config_fingerprint = configuration_fingerprint(
            config.parameters,
            simulation_account_id=config.simulation_account_id,
        )
        db.commit()
        monkeypatch.setattr(
            "app.probability_portfolio.execution.daily_pnl_pct",
            lambda *args, **kwargs: -0.11,
        )

        run = execute_portfolio_entry(
            db,
            config,
            current=CURRENT,
            scored_candidates=scored(stock_ids[:1]),
        )

        assert run.summary["accepted"] == 0
        assert "日亏损" in run.summary["reason"]
        assert db.scalar(
            select(func.count(Order.id)).where(Order.account_id == account_id)
        ) == 0


def test_dry_run_ignores_daily_loss_without_creating_orders(tmp_path):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=True)
    with Session(engine) as db:
        from app.models import StrategyConfig

        account = db.get(SimulationAccount, account_id)
        account.cash_balance = 1_960_000
        account.available_cash = 1_960_000
        account.total_asset = 1_960_000
        db.commit()
        config = db.get(StrategyConfig, config_id)

        run = execute_portfolio_entry(
            db,
            config,
            current=CURRENT,
            scored_candidates=scored(stock_ids[:1]),
        )

        assert run.summary["dry_run"] is True
        assert run.summary["selected"] == 1
        assert run.summary["accepted"] == 0
        assert db.scalar(
            select(func.count(Order.id)).where(Order.account_id == account_id)
        ) == 0


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
        assert db.scalar(
            select(func.count(ProbabilityPortfolioRun.id)).where(
                ProbabilityPortfolioRun.trigger_type == "portfolio_entry"
            )
        ) == 1


def test_entry_enforces_hard_position_limits_even_if_database_config_is_unsafe(
    tmp_path,
):
    engine, config_id, _, stock_ids = setup_runtime(tmp_path, dry_run=True)
    with Session(engine) as db:
        from app.models import StrategyConfig

        config = db.get(StrategyConfig, config_id)
        config.parameters = {
            **config.parameters,
            "max_positions": 99,
            "min_position_pct": 0.0,
            "max_position_pct": 1.0,
            "min_total_exposure_pct": 1.0,
            "max_total_exposure_pct": 1.0,
        }
        db.commit()

        run = execute_portfolio_entry(
            db,
            config,
            current=CURRENT,
            scored_candidates=scored(stock_ids),
        )
        decisions = list(
            db.scalars(
                select(ProbabilityCandidateDecision).where(
                    ProbabilityCandidateDecision.portfolio_run_id
                    == run.summary["portfolio_run_id"]
                )
            )
        )

        assert len(decisions) <= 10
        assert all((item.target_weight or 0) <= 0.36 for item in decisions)
        assert sum(item.target_weight or 0 for item in decisions) <= 0.60


def test_entry_keeps_probability_and_positive_expectation_hard_gates(tmp_path):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=True)
    with Session(engine) as db:
        from app.models import StrategyConfig

        config = db.get(StrategyConfig, config_id)
        config.parameters = {
            **config.parameters,
            "min_probability": 0.01,
            "min_expected_net_return": -1.0,
        }
        db.commit()
        weak = ScoredCandidate(
            stock_id=stock_ids[0],
            symbol="600001.SH",
            features={"volatility_20d": 0.10},
            raw_probability=0.90,
            calibrated_probability=0.90,
            expected_net_return=-0.20,
            volatility_20d=0.10,
        )

        run = execute_portfolio_entry(
            db,
            config,
            current=CURRENT,
            scored_candidates=[weak],
        )

        assert run.summary["selected"] == 0
        assert run.summary["accepted"] == 0
        assert db.scalar(
            select(func.count(Order.id)).where(Order.account_id == account_id)
        ) == 0


def test_entry_blocks_new_buys_while_previous_strategy_lots_are_open(tmp_path):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=False)
    _open_lots(engine, config_id, stock_ids)
    next_entry = datetime(2026, 7, 24, 14, 40, 10, tzinfo=SHANGHAI)
    with Session(engine) as db:
        from app.models import StrategyConfig

        config = db.get(StrategyConfig, config_id)
        before = db.scalar(
            select(func.count(Order.id)).where(
                Order.account_id == account_id,
                Order.side == "buy",
            )
        )

        run = execute_portfolio_entry(
            db,
            config,
            current=next_entry,
            scored_candidates=scored(stock_ids[2:]),
        )

        after = db.scalar(
            select(func.count(Order.id)).where(
                Order.account_id == account_id,
                Order.side == "buy",
            )
        )
        assert run.summary["accepted"] == 0
        assert "未退出持仓" in run.summary["reason"]
        assert after == before


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


def test_exit_rechecks_simulation_runtime_gate_before_any_sell(
    tmp_path,
    monkeypatch,
):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=False)
    _open_lots(engine, config_id, stock_ids)
    exit_at = datetime(2026, 7, 24, 10, 30, 5, tzinfo=SHANGHAI)
    with Session(engine) as db:
        from app.models import StrategyConfig

        config = db.get(StrategyConfig, config_id)
        monkeypatch.setattr(
            "app.probability_portfolio.execution.get_settings",
            lambda: SimpleNamespace(live_enabled=False, broker_adapter="non_simulation"),
        )

        with pytest.raises(ValueError, match="仅允许模拟盘"):
            execute_portfolio_exit(db, config, current=exit_at)

        assert db.scalar(
            select(func.count(Order.id)).where(
                Order.account_id == account_id,
                Order.side == "sell",
            )
        ) == 0


def test_exit_refuses_to_sell_more_than_the_aggregate_position(tmp_path):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=False)
    _open_lots(engine, config_id, stock_ids)
    exit_at = datetime(2026, 7, 24, 10, 30, 5, tzinfo=SHANGHAI)
    with Session(engine) as db:
        from app.models import StrategyConfig

        position = db.scalar(
            select(Position).where(
                Position.account_id == account_id,
                Position.stock_id == stock_ids[0],
            )
        )
        position.quantity -= 100
        for stock_id in stock_ids[:2]:
            stock = db.get(Stock, stock_id)
            stock.quote_updated_at = exit_at - timedelta(seconds=5)
        db.commit()
        config = db.get(StrategyConfig, config_id)

        run = execute_portfolio_exit(db, config, current=exit_at)
        sells = db.scalar(
            select(func.count(Order.id)).where(
                Order.account_id == account_id,
                Order.side == "sell",
            )
        )

        assert run.summary["accepted"] == 0
        assert run.summary["retryable"] is True
        assert "可卖数量不足" in run.summary["reason"]
        assert sells == 0


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

        for stock_id in stock_ids[:2]:
            stock = db.get(Stock, stock_id)
            stock.quote_updated_at = exit_at + timedelta(seconds=10)
        db.commit()
        recovered = execute_portfolio_exit(
            db,
            config,
            current=exit_at + timedelta(seconds=15),
        )

        assert recovered.summary["accepted"] == 2


def test_exit_retries_without_any_fill_when_one_stock_is_not_tradable(tmp_path):
    engine, config_id, account_id, stock_ids = setup_runtime(tmp_path, dry_run=False)
    _open_lots(engine, config_id, stock_ids)
    exit_at = datetime(2026, 7, 24, 10, 30, 5, tzinfo=SHANGHAI)
    with Session(engine) as db:
        from app.models import StrategyConfig

        for stock_id in stock_ids[:2]:
            stock = db.get(Stock, stock_id)
            stock.quote_updated_at = exit_at - timedelta(seconds=5)
            stock.limit_down_price = stock.last_price - 1
        blocked = db.get(Stock, stock_ids[0])
        blocked.limit_down_price = blocked.last_price
        config = db.get(StrategyConfig, config_id)

        run = execute_portfolio_exit(db, config, current=exit_at)
        sells = db.scalar(
            select(func.count(Order.id)).where(
                Order.account_id == account_id,
                Order.side == "sell",
            )
        )

        assert run.summary["accepted"] == 0
        assert run.summary["retryable"] is True
        assert "不可交易" in run.summary["reason"]
        assert sells == 0


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
