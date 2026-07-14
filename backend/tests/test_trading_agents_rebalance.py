from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import (
    AccountSnapshot,
    Order,
    Position,
    RiskSettings,
    SimulationAccount,
    Stock,
    StrategyConfig,
    StrategyDefinition,
    TradingAgentBatch,
    TradingAgentPortfolioDecision,
    RiskEvent,
)
from app.services import seed_database
from app.trading_agents.rebalance import rebalance_batch, revalue_simulation_account
from app.trading_agents.runtime import seed_trading_agents_runtime
from app.trading_agents.config import configuration_fingerprint


SHANGHAI = ZoneInfo("Asia/Shanghai")


def setup_rebalance_db(tmp_path: Path, *, dry_run: bool = False):
    engine = create_engine(f"sqlite:///{tmp_path / 'rebalance.db'}")
    Base.metadata.create_all(engine)
    current = datetime(2026, 7, 14, 14, 46, tzinfo=SHANGHAI)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=str(engine.url)))
        seed_trading_agents_runtime(db, Settings(database_url=str(engine.url)))
        config = db.scalar(
            select(StrategyConfig).join(StrategyDefinition).where(
                StrategyDefinition.key == "trading_agents_auto"
            )
        )
        config.parameters = {**config.parameters, "dry_run": dry_run}
        risk = db.scalar(select(RiskSettings).where(RiskSettings.mode == "SIMULATION"))
        risk.max_order_notional_abs = 20_000
        account = db.get(SimulationAccount, config.simulation_account_id)
        stocks = []
        for index in range(3):
            symbol = f"60020{index}.SH"
            stock = Stock(
                code=symbol[:6],
                exchange="SSE",
                symbol=symbol,
                name=f"调仓{index}",
                status="active",
                last_price=10,
                turnover_amount=300_000_000,
                quote_updated_at=current - timedelta(seconds=30),
            )
            db.add(stock)
            db.flush()
            stocks.append(stock)
        batch = TradingAgentBatch(
            strategy_config_id=config.id,
            simulation_account_id=account.id,
            trading_date=current.date().isoformat(),
            status="ready",
            analysis_profile="a_share_balanced",
            position_mapping="fixed_rating",
            quick_model="gpt-5.4-mini",
            deep_model="gpt-5.2",
            config_fingerprint=configuration_fingerprint(
                config.parameters,
                simulation_account_id=account.id,
            ),
            candidate_symbols=[item.symbol for item in stocks],
            holding_symbols=[],
            required_symbols=[item.symbol for item in stocks],
            analysis_deadline=current - timedelta(minutes=4),
            rebalance_after=current - timedelta(minutes=1),
        )
        db.add(batch)
        db.flush()
        db.add(
            TradingAgentPortfolioDecision(
                batch_id=batch.id,
                status="ready",
                position_mapping="fixed_rating",
                target_weights={stocks[0].symbol: 0.20, stocks[1].symbol: 0.10},
                rankings=[],
                rationale="test",
                model="gpt-5.2",
            )
        )
        db.commit()
        return engine, current, batch.id, account.id, [item.symbol for item in stocks]


def test_rebalance_executes_separate_account_and_is_idempotent(tmp_path):
    engine, current, batch_id, account_id, symbols = setup_rebalance_db(tmp_path)
    with Session(engine) as db:
        batch = db.get(TradingAgentBatch, batch_id)
        run = rebalance_batch(db, batch, current=current)
        first_order_count = db.scalar(
            select(func.count(Order.id)).where(Order.account_id == account_id)
        )
        second = rebalance_batch(db, batch, current=current)
        account = db.get(SimulationAccount, account_id)
        positions = list(
            db.scalars(
                select(Position).where(
                    Position.account_id == account_id,
                    Position.quantity > 0,
                )
            )
        )

    assert run.status == "completed"
    assert batch.status == "rebalanced"
    assert first_order_count == 2
    assert second.id == run.id
    assert {item.stock_id for item in positions}
    assert len(positions) == 2
    assert account.cash_balance < 100_000
    assert account.total_asset == account.cash_balance + sum(item.market_value for item in positions)
    assert set(batch.order_ids) and set(symbols[:2]) == set(run.summary["target_weights"])


def test_dry_run_writes_no_orders_and_marks_validation(tmp_path):
    engine, current, batch_id, account_id, _ = setup_rebalance_db(tmp_path, dry_run=True)
    with Session(engine) as db:
        batch = db.get(TradingAgentBatch, batch_id)
        run = rebalance_batch(db, batch, current=current)
        orders = db.scalar(select(func.count(Order.id)).where(Order.account_id == account_id))
        run_status = run.status
        run_summary = dict(run.summary)
        batch_status = batch.status

    assert run_status == "completed"
    assert run_summary["dry_run"] is True
    assert batch_status == "dry_run_completed"
    assert orders == 0


def test_rebalance_records_target_differences_smaller_than_one_lot(tmp_path):
    engine, current, batch_id, _, symbols = setup_rebalance_db(tmp_path, dry_run=True)
    with Session(engine) as db:
        decision = db.scalar(
            select(TradingAgentPortfolioDecision).where(
                TradingAgentPortfolioDecision.batch_id == batch_id
            )
        )
        decision.target_weights = {symbols[0]: 0.005}
        db.commit()
        batch = db.get(TradingAgentBatch, batch_id)
        run = rebalance_batch(db, batch, current=current)
        summary = dict(run.summary)

    assert summary["accepted"] == 0
    assert summary["skipped_orders"] == [
        {
            "symbol": symbols[0],
            "reason": "目标差额不足一手",
            "target_weight": 0.005,
        }
    ]


def test_any_unavailable_t_plus_one_sell_blocks_entire_batch(tmp_path):
    engine, current, batch_id, account_id, symbols = setup_rebalance_db(tmp_path)
    with Session(engine) as db:
        first = db.scalar(select(Stock).where(Stock.symbol == symbols[0]))
        position = Position(
            account_id=account_id,
            mode="SIMULATION",
            stock_id=first.id,
            quantity=1000,
            available_quantity=0,
            average_cost=10,
            market_value=10_000,
            unrealized_pnl=0,
        )
        db.add(position)
        decision = db.scalar(
            select(TradingAgentPortfolioDecision).where(
                TradingAgentPortfolioDecision.batch_id == batch_id
            )
        )
        decision.target_weights = {symbols[1]: 0.20}
        db.commit()
        batch = db.get(TradingAgentBatch, batch_id)
        run = rebalance_batch(db, batch, current=current)
        orders = db.scalar(select(func.count(Order.id)).where(Order.account_id == account_id))
        run_summary = dict(run.summary)
        batch_status = batch.status

    assert run_summary["accepted"] == 0
    assert "T+1" in run_summary["reason"]
    assert batch_status == "blocked"
    assert orders == 0


def test_revalue_uses_cash_plus_all_position_market_values(tmp_path):
    engine, _, _, account_id, symbols = setup_rebalance_db(tmp_path)
    with Session(engine) as db:
        account = db.get(SimulationAccount, account_id)
        for symbol in symbols[:2]:
            stock = db.scalar(select(Stock).where(Stock.symbol == symbol))
            db.add(
                Position(
                    account_id=account_id,
                    mode="SIMULATION",
                    stock_id=stock.id,
                    quantity=100,
                    available_quantity=100,
                    average_cost=9,
                    market_value=0,
                    unrealized_pnl=0,
                )
            )
        account.cash_balance = 80_000
        account.available_cash = 80_000
        db.flush()
        revalue_simulation_account(db, account)

    assert account.total_asset == 82_000
    assert account.unrealized_pnl == 200


def test_daily_loss_uses_first_account_snapshot_of_trading_day(tmp_path):
    engine, current, batch_id, account_id, _ = setup_rebalance_db(tmp_path)
    with Session(engine) as db:
        account = db.get(SimulationAccount, account_id)
        account.cash_balance = 96_000
        account.available_cash = 96_000
        account.total_asset = 96_000
        db.add(
            AccountSnapshot(
                mode="SIMULATION",
                account_id=account_id,
                cash_balance=100_000,
                available_cash=100_000,
                frozen_cash=0,
                market_value=0,
                total_asset=100_000,
                realized_pnl=0,
                unrealized_pnl=0,
                exposure=0,
                source="test",
                captured_at=current.replace(hour=9, minute=30),
            )
        )
        db.commit()
        batch = db.get(TradingAgentBatch, batch_id)
        run = rebalance_batch(db, batch, current=current)
        order_count = db.scalar(
            select(func.count(Order.id)).where(Order.account_id == account_id)
        )
        summary = dict(run.summary)

    assert summary["accepted"] == 0
    assert "日亏损" in summary["reason"]
    assert order_count == 0


def test_consecutive_recent_batch_failures_pause_rebalance(tmp_path):
    engine, current, batch_id, account_id, _ = setup_rebalance_db(tmp_path)
    with Session(engine) as db:
        risk = db.scalar(select(RiskSettings).where(RiskSettings.mode == "SIMULATION"))
        for index in range(risk.max_consecutive_errors):
            db.add(
                RiskEvent(
                    mode="SIMULATION",
                    event_type="trading_agents_batch_failure",
                    message=f"failure-{index}",
                    context={"simulation_account_id": account_id},
                    created_at=current - timedelta(minutes=index + 1),
                )
            )
        db.commit()
        batch = db.get(TradingAgentBatch, batch_id)
        run = rebalance_batch(db, batch, current=current)
        summary = dict(run.summary)

    assert summary["accepted"] == 0
    assert "连续错误" in summary["reason"]


def test_rebalance_blocks_when_configuration_changed_after_analysis(tmp_path):
    engine, current, batch_id, account_id, _ = setup_rebalance_db(tmp_path)
    with Session(engine) as db:
        batch = db.get(TradingAgentBatch, batch_id)
        config = db.get(StrategyConfig, batch.strategy_config_id)
        config.parameters = {**config.parameters, "deep_model": "gpt-5.4"}
        db.commit()

        run = rebalance_batch(db, batch, current=current)
        order_count = db.scalar(
            select(func.count(Order.id)).where(Order.account_id == account_id)
        )
        summary = dict(run.summary)

    assert summary["accepted"] == 0
    assert "配置" in summary["reason"]
    assert order_count == 0
