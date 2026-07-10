from __future__ import annotations

import time

from sqlalchemy import select

from .config import get_settings
from .database import Base, SessionLocal, engine
from .data_sync import refresh_quotes
from .models import NotificationChannel, NotificationDelivery, Stock, WatchlistItem, now
from .notifications import deliver_channel
from .providers import market_router
from .services import seed_database


def process_pending_notifications(limit: int = 20) -> int:
    processed = 0
    settings = get_settings()
    with SessionLocal() as db:
        pending = list(
            db.scalars(
                select(NotificationDelivery)
                .where(NotificationDelivery.status == "pending")
                .order_by(NotificationDelivery.id)
                .limit(limit)
            )
        )
        for delivery in pending:
            channel = db.get(NotificationChannel, delivery.channel_id)
            if not channel or not channel.enabled:
                delivery.status = "failed"
                delivery.last_error = "通知渠道不存在或未启用"
                processed += 1
                continue
            result = deliver_channel(
                settings,
                channel_type=channel.type,
                recipient=channel.recipient,
                secret_ref=channel.secret_ref,
                subject=delivery.subject,
                message=str(delivery.payload.get("message", delivery.payload)),
            )
            delivery.status = "sent" if result.sent else "failed"
            delivery.attempt_count = result.attempt_count
            delivery.last_error = result.last_error
            delivery.sent_at = now() if result.sent else None
            processed += 1
        db.commit()
    return processed


def poll_watchlist_quotes(provider=None, session_factory=SessionLocal) -> dict[str, int]:
    updated = 0
    missing = 0
    errors = 0
    with session_factory() as db:
        symbols = [
            item.symbol
            for item in db.scalars(
                select(Stock).join(WatchlistItem, WatchlistItem.stock_id == Stock.id).order_by(WatchlistItem.id)
            )
        ]
        if not symbols:
            return {"updated": 0, "missing": 0, "errors": 0}
        try:
            selected_provider = provider or market_router().select("realtime")
            result = refresh_quotes(db, selected_provider, symbols)
            updated = result.updated
            missing = len(result.missing)
        except Exception:
            updated = 0
            missing = len(symbols)
            errors = 1
    return {"updated": updated, "missing": missing, "errors": errors}


def main() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_database(db, get_settings())
    last_quote_poll = 0.0
    while True:
        process_pending_notifications()
        current = time.time()
        if current - last_quote_poll >= get_settings().realtime_poll_seconds:
            poll_watchlist_quotes()
            last_quote_poll = current
        time.sleep(2)


if __name__ == "__main__":
    main()
