from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import BrokerGateway, GatewayEvent, LiveTradingAccount
from app.services import seed_database


class HealthyAdapter:
    name = "test-broker"

    def health(self):
        from app.brokers import BrokerHealth

        return BrokerHealth(True, "ok", ("accounts", "orders", "positions"))

    def query_accounts(self):
        return [{"account_id": "1234567890", "alias": "主账户", "currency": "CNY", "read_only": False, "markets": ["A_SHARE"], "capabilities": ["orders", "positions"]}]


def test_live_account_sync_persists_only_masked_number(tmp_path: Path):
    from app.live_trading import sync_live_accounts

    engine = create_engine(f"sqlite:///{tmp_path / 'live.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{tmp_path / 'live.db'}"))
        gateway = db.scalar(select(BrokerGateway).where(BrokerGateway.type == "qmt"))
        gateway.enabled = True
        db.commit()

        result = sync_live_accounts(db, gateway, HealthyAdapter())

        account = db.scalar(select(LiveTradingAccount))
        assert result.synced == 1
        assert account.account_no_masked == "******7890"
        assert "123456" not in account.account_no_masked
        assert not account.enabled
        assert not account.read_only
        assert account.market_permissions == ["A_SHARE"]
        assert account.account_capabilities == ["orders", "positions"]
        assert db.scalar(select(GatewayEvent).where(GatewayEvent.event_type == "account_sync")) is not None
