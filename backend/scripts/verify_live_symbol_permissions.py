from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import AccountSnapshot, BrokerGateway, LiveTradingAccount, RiskSettings, StrategyConfig, StrategyDefinition, Stock, WatchlistItem
from app.services import execute_simulation_strategy, seed_database


def main() -> None:
    db_path = Path(__file__).resolve().parents[1] / ".tmp-live-symbols.db"
    if db_path.exists():
        db_path.unlink()
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{db_path}"))
        definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
        live_risk = db.scalar(select(RiskSettings).where(RiskSettings.mode == "LIVE"))
        gateway = db.scalar(select(BrokerGateway).where(BrokerGateway.type == "qmt"))
        stock = db.scalar(select(Stock).where(Stock.symbol == "000001.SZ"))

        live_risk.live_enabled = True
        gateway.enabled = True
        gateway.healthy = True
        stock.quote_updated_at = stock.created_at
        db.add(WatchlistItem(stock_id=stock.id))
        account = LiveTradingAccount(
            broker="qmt",
            account_alias="受限账户",
            account_no_masked="******1234",
            gateway_id=gateway.id,
            enabled=True,
            read_only=False,
            market_permissions=["HK", "US"],
            account_capabilities=["orders"],
        )
        db.add(account)
        db.flush()
        db.add(
            AccountSnapshot(
                mode="LIVE",
                account_id=account.id,
                cash_balance=200000,
                available_cash=200000,
                frozen_cash=0,
                market_value=0,
                total_asset=200000,
                realized_pnl=0,
                unrealized_pnl=0,
                exposure=0,
                source="test",
            )
        )
        config = StrategyConfig(strategy_definition_id=definition.id, name="permission-check", mode="LIVE", parameters={})
        db.add(config)
        db.commit()

        blocked = execute_simulation_strategy(db, config)
        assert blocked.status == "failed"
        assert "A 股交易权限" in blocked.error_message

    print("live_symbol_permissions_ok")


if __name__ == "__main__":
    main()
