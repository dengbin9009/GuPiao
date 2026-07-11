from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .brokers import BrokerAdapter
from .models import BrokerGateway, GatewayEvent, LiveTradingAccount, now


@dataclass(frozen=True)
class AccountSyncResult:
    synced: int
    gateway_healthy: bool


def mask_account_number(account_id: str) -> str:
    value = str(account_id).strip()
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


def sync_live_accounts(
    db: Session,
    gateway: BrokerGateway,
    adapter: BrokerAdapter,
) -> AccountSyncResult:
    health = adapter.health()
    gateway.healthy = health.healthy
    gateway.last_checked_at = now()
    gateway.last_error = None if health.healthy else health.message
    if not health.healthy:
        db.add(
            GatewayEvent(
                gateway_id=gateway.id,
                event_type="offline",
                message=health.message,
                context={"adapter": adapter.name},
            )
        )
        db.commit()
        return AccountSyncResult(0, False)

    synced = 0
    for raw in adapter.query_accounts():
        account_id = str(raw.get("account_id") or raw.get("account_no") or "")
        if not account_id:
            continue
        masked = mask_account_number(account_id)
        account = db.scalar(
            select(LiveTradingAccount).where(
                LiveTradingAccount.gateway_id == gateway.id,
                LiveTradingAccount.account_no_masked == masked,
            )
        )
        if account is None:
            account = LiveTradingAccount(
                broker=gateway.type,
                account_alias=str(raw.get("alias") or gateway.name),
                account_no_masked=masked,
                gateway_id=gateway.id,
                market_permissions=list(raw.get("markets") or ["A_SHARE"]),
                account_capabilities=list(raw.get("capabilities") or list(health.capabilities)),
                enabled=False,
                read_only=bool(raw.get("read_only", True)),
            )
            db.add(account)
        else:
            account.account_alias = str(raw.get("alias") or account.account_alias)
            account.read_only = bool(raw.get("read_only", account.read_only))
            account.market_permissions = list(raw.get("markets") or account.market_permissions or ["A_SHARE"])
            account.account_capabilities = list(raw.get("capabilities") or account.account_capabilities or list(health.capabilities))
        account.currency = str(raw.get("currency") or "CNY")
        account.last_synced_at = now()
        synced += 1
    db.add(
        GatewayEvent(
            gateway_id=gateway.id,
            event_type="account_sync",
            message=f"同步真实盘账户 {synced} 个",
            context={"adapter": adapter.name, "capabilities": list(health.capabilities)},
        )
    )
    db.commit()
    return AccountSyncResult(synced, True)
