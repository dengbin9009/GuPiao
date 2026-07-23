from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AccountSnapshot, Position, SimulationAccount, Stock


@dataclass(frozen=True)
class AccountValuation:
    market_value: float
    unrealized_pnl: float
    total_asset: float


def revalue_account(db: Session, account: SimulationAccount) -> AccountValuation:
    db.flush()
    market_value = 0.0
    unrealized_pnl = 0.0
    all_positions = list(
        db.scalars(
            select(Position).where(
                Position.account_id == account.id,
                Position.mode == "SIMULATION",
            )
        )
    )
    for position in all_positions:
        if position.quantity <= 0:
            position.market_value = 0.0
            position.unrealized_pnl = 0.0
            continue
        stock = db.get(Stock, position.stock_id)
        price = float(stock.last_price or 0) if stock else 0.0
        position.market_value = position.quantity * price
        position.unrealized_pnl = (
            price - float(position.average_cost)
        ) * position.quantity
        market_value += position.market_value
        unrealized_pnl += position.unrealized_pnl
    account.unrealized_pnl = unrealized_pnl
    account.total_asset = float(account.cash_balance) + market_value
    return AccountValuation(market_value, unrealized_pnl, account.total_asset)


def snapshot_account(
    db: Session,
    account: SimulationAccount,
    *,
    source: str = "simulated_broker",
) -> AccountSnapshot:
    valuation = revalue_account(db, account)
    snapshot = AccountSnapshot(
        mode="SIMULATION",
        account_id=account.id,
        cash_balance=account.cash_balance,
        available_cash=account.available_cash,
        frozen_cash=account.frozen_cash,
        market_value=valuation.market_value,
        total_asset=valuation.total_asset,
        realized_pnl=account.realized_pnl,
        unrealized_pnl=valuation.unrealized_pnl,
        exposure=(
            valuation.market_value / valuation.total_asset
            if valuation.total_asset
            else 0
        ),
        source=source,
    )
    db.add(snapshot)
    return snapshot


def daily_pnl_pct(
    db: Session,
    account: SimulationAccount,
    *,
    current: datetime,
) -> float:
    day_start = datetime.combine(current.date(), datetime.min.time())
    day_end = current.replace(tzinfo=None) if current.tzinfo else current
    opening_snapshot = db.scalar(
        select(AccountSnapshot)
        .where(
            AccountSnapshot.mode == "SIMULATION",
            AccountSnapshot.account_id == account.id,
            AccountSnapshot.captured_at >= day_start,
            AccountSnapshot.captured_at <= day_end,
        )
        .order_by(AccountSnapshot.captured_at, AccountSnapshot.id)
        .limit(1)
    )
    if opening_snapshot is None:
        opening_snapshot = db.scalar(
            select(AccountSnapshot)
            .where(
                AccountSnapshot.mode == "SIMULATION",
                AccountSnapshot.account_id == account.id,
                AccountSnapshot.captured_at < day_start,
            )
            .order_by(AccountSnapshot.captured_at.desc(), AccountSnapshot.id.desc())
            .limit(1)
        )
    opening_asset = (
        float(opening_snapshot.total_asset)
        if opening_snapshot and opening_snapshot.total_asset > 0
        else float(account.initial_cash)
    )
    if opening_asset <= 0:
        return 0.0
    return (float(account.total_asset) - opening_asset) / opening_asset
