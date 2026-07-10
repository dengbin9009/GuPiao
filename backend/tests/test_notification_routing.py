from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import NotificationChannel, NotificationDelivery
from app.notifications import deliver_with_retries
from app.services import seed_database


def test_notification_routing_redacts_sensitive_payload(tmp_path: Path):
    from app.notifications import queue_notifications

    engine = create_engine(f"sqlite:///{tmp_path / 'notify.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_database(db, Settings(database_url=f"sqlite:///{tmp_path / 'notify.db'}"))
        db.add_all(
            [
                NotificationChannel(type="email", name="邮件", enabled=True, recipient="ops@example.com", secret_ref="SMTP_PASSWORD", event_types=["risk_block"]),
                NotificationChannel(type="wecom", name="企微", enabled=True, recipient="交易群", secret_ref="WECOM_WEBHOOK_URL", event_types=["risk_block"]),
            ]
        )
        db.commit()

        count = queue_notifications(
            db,
            event_type="risk_block",
            severity="critical",
            subject="订单被拦截",
            payload={"symbol": "000001.SZ", "token": "secret-token", "account_id": "1234567890"},
        )

        deliveries = list(db.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id)))
        assert count == 2
        assert all(item.status == "pending" for item in deliveries)
        assert all(item.payload["token"] == "***" for item in deliveries)
        assert all(item.payload["account_id"] == "******7890" for item in deliveries)


def test_notification_final_failure_is_bounded():
    attempts = 0

    def always_fail():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("offline")

    result = deliver_with_retries(always_fail, max_attempts=3)

    assert not result.sent
    assert result.attempt_count == 3
    assert attempts == 3
