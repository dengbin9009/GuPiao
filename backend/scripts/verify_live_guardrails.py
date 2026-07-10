from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import AccountSnapshot, BrokerGateway, LiveTradingAccount, RiskSettings, StrategyConfig, StrategyDefinition
from app.services import execute_simulation_strategy, seed_database
from app.brokers import BrokerHealth


def main() -> None:
    db_path = Path(__file__).resolve().parents[1] / ".tmp-live-verify.db"
    if db_path.exists():
        db_path.unlink()
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{db_path}"))
        definition = db.scalar(select(StrategyDefinition).where(StrategyDefinition.key == "overnight_hold"))
        live_risk = db.scalar(select(RiskSettings).where(RiskSettings.mode == "LIVE"))
        gateway = db.scalar(select(BrokerGateway).where(BrokerGateway.type == "qmt"))

        config = StrategyConfig(strategy_definition_id=definition.id, name="live-check", mode="LIVE", parameters={})
        db.add(config)
        db.commit()

        live_risk.live_enabled = True
        db.commit()
        blocked = execute_simulation_strategy(db, config)
        assert blocked.status == "failed"
        assert "真实盘账户" in blocked.error_message

        gateway.enabled = True
        gateway.healthy = True
        account = LiveTradingAccount(
            broker="qmt",
            account_alias="主账户",
            account_no_masked="******7890",
            gateway_id=gateway.id,
            enabled=True,
            read_only=False,
            market_permissions=["A_SHARE"],
            account_capabilities=["orders", "positions"],
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
        db.commit()

        account.read_only = True
        db.commit()
        readonly = execute_simulation_strategy(db, config)
        assert readonly.status == "failed"
        assert "只读" in readonly.error_message

        account.read_only = False
        gateway.healthy = False
        db.commit()
        unhealthy = execute_simulation_strategy(db, config)
        assert unhealthy.status == "failed"
        assert "网关" in unhealthy.error_message

        class HealthyGatewayAdapter:
            name = "QMT"

            def health(self):
                return BrokerHealth(True, "ok", ("accounts", "orders"))

            def query_accounts(self):
                return []

            def place_order(self, order):
                return {"broker_order_id": "BRK-1", "status": "submitted", "echo": order}

        gateway.healthy = True
        db.commit()

        import app.services as services

        original_build = services.build_broker_adapter
        services.build_broker_adapter = lambda *args, **kwargs: HealthyGatewayAdapter()
        try:
            stock = db.scalar(select(services.Stock).where(services.Stock.symbol == "000001.SZ"))
            stock.quote_updated_at = services.now()
            success = execute_simulation_strategy(db, config)
            assert success.status == "completed"
            assert success.summary["broker_order_id"] == "BRK-1"
        finally:
            services.build_broker_adapter = original_build

        print("live_guardrails_ok")


if __name__ == "__main__":
    main()
