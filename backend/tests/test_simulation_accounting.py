from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import AccountSnapshot, Position, SimulationAccount, Stock
from app.services import seed_database
from app.simulation_accounts import daily_pnl_pct, revalue_account, snapshot_account


SHANGHAI = ZoneInfo("Asia/Shanghai")


def setup_account(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'accounting.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=str(engine.url)))
    return engine


def test_snapshot_flushes_new_position_and_counts_market_value(tmp_path):
    engine = setup_account(tmp_path)
    with Session(engine, autoflush=False) as db:
        account = db.scalar(select(SimulationAccount).order_by(SimulationAccount.id))
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        stock.last_price = 12
        account.cash_balance = 8_000
        account.available_cash = 8_000
        db.add(
            Position(
                account_id=account.id,
                mode="SIMULATION",
                stock_id=stock.id,
                quantity=100,
                available_quantity=0,
                average_cost=10,
                market_value=0,
                unrealized_pnl=0,
            )
        )

        snapshot = snapshot_account(db, account, source="test")

        assert snapshot.market_value == 1_200
        assert snapshot.total_asset == 9_200
        assert account.total_asset == 9_200
        assert account.unrealized_pnl == 200


def test_revalue_excludes_zero_quantity_positions(tmp_path):
    engine = setup_account(tmp_path)
    with Session(engine) as db:
        account = db.scalar(select(SimulationAccount).order_by(SimulationAccount.id))
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))
        stock.last_price = 12
        account.cash_balance = 10_500
        db.add(
            Position(
                account_id=account.id,
                mode="SIMULATION",
                stock_id=stock.id,
                quantity=0,
                available_quantity=0,
                average_cost=0,
                market_value=99_999,
                unrealized_pnl=99_999,
            )
        )

        result = revalue_account(db, account)

        assert result.market_value == 0
        assert account.total_asset == 10_500
        assert account.unrealized_pnl == 0


def test_daily_pnl_uses_first_snapshot_today_not_lifetime_realized_pnl(tmp_path):
    engine = setup_account(tmp_path)
    current = datetime(2026, 7, 23, 14, 40, tzinfo=SHANGHAI)
    with Session(engine) as db:
        account = db.scalar(select(SimulationAccount).order_by(SimulationAccount.id))
        account.initial_cash = 100_000
        account.cash_balance = 95_000
        account.total_asset = 99_000
        account.realized_pnl = -5_000
        db.add(
            AccountSnapshot(
                mode="SIMULATION",
                account_id=account.id,
                cash_balance=100_000,
                available_cash=100_000,
                frozen_cash=0,
                market_value=0,
                total_asset=100_000,
                realized_pnl=-4_000,
                unrealized_pnl=0,
                exposure=0,
                source="test",
                captured_at=current.replace(hour=9, minute=30),
            )
        )
        db.add(
            AccountSnapshot(
                mode="SIMULATION",
                account_id=account.id,
                cash_balance=99_500,
                available_cash=99_500,
                frozen_cash=0,
                market_value=0,
                total_asset=99_500,
                realized_pnl=-4_500,
                unrealized_pnl=0,
                exposure=0,
                source="test",
                captured_at=current - timedelta(hours=1),
            )
        )
        db.commit()

        value = daily_pnl_pct(db, account, current=current)

        assert value == pytest.approx(-0.01)


def test_daily_pnl_falls_back_to_initial_cash_without_today_snapshot(tmp_path):
    engine = setup_account(tmp_path)
    current = datetime(2026, 7, 23, 14, 40, tzinfo=SHANGHAI)
    with Session(engine) as db:
        account = db.scalar(select(SimulationAccount).order_by(SimulationAccount.id))
        account.initial_cash = 100_000
        account.total_asset = 98_000

        assert daily_pnl_pct(db, account, current=current) == pytest.approx(-0.02)

