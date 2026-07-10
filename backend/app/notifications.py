from __future__ import annotations

import json
import os
import smtplib
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import NotificationChannel, NotificationDelivery


@dataclass(frozen=True)
class DeliveryResult:
    sent: bool
    attempt_count: int
    last_error: str | None


def _redact(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            lowered = key.lower()
            if any(word in lowered for word in ("password", "token", "secret", "webhook")):
                result[key] = "***"
            elif "account" in lowered and isinstance(item, str):
                result[key] = "*" * max(0, len(item) - 4) + item[-4:]
            else:
                result[key] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def queue_notifications(
    db: Session,
    *,
    event_type: str,
    severity: str,
    subject: str,
    payload: dict,
) -> int:
    channels = list(
        db.scalars(select(NotificationChannel).where(NotificationChannel.enabled.is_(True)))
    )
    redacted = _redact(payload)
    queued = 0
    for channel in channels:
        if event_type not in (channel.event_types or []):
            continue
        db.add(
            NotificationDelivery(
                channel_id=channel.id,
                event_type=event_type,
                severity=severity,
                subject=subject,
                payload=redacted,
                status="pending",
            )
        )
        queued += 1
    db.commit()
    return queued


def deliver_with_retries(sender: Callable[[], None], max_attempts: int = 3) -> DeliveryResult:
    attempts = max(1, max_attempts)
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            sender()
            return DeliveryResult(True, attempt, None)
        except Exception as exc:  # Delivery boundaries normalize provider failures.
            last_error = str(exc)
    return DeliveryResult(False, attempts, last_error)


def send_email(settings: Settings, recipient: str, subject: str, message: str) -> None:
    if not settings.smtp_host or not settings.smtp_from:
        raise RuntimeError("SMTP 未配置")
    email = EmailMessage()
    email["From"] = settings.smtp_from
    email["To"] = recipient
    email["Subject"] = subject
    email.set_content(message)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=5) as smtp:
        smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(email)


def send_wecom(webhook_url: str, message: str) -> None:
    if not webhook_url:
        raise RuntimeError("企业微信 Webhook 未配置")
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps({"msgtype": "text", "text": {"content": message}}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status >= 300:
            raise RuntimeError(f"企业微信返回 HTTP {response.status}")


def deliver_channel(
    settings: Settings,
    *,
    channel_type: str,
    recipient: str,
    secret_ref: str,
    subject: str,
    message: str,
) -> DeliveryResult:
    if channel_type == "email":
        sender = lambda: send_email(settings, recipient, subject, message)
    elif channel_type == "wecom":
        webhook_url = os.getenv(secret_ref, "") if secret_ref else ""
        webhook_url = webhook_url or settings.wecom_webhook_url
        sender = lambda: send_wecom(webhook_url, f"{subject}\n{message}")
    else:
        return DeliveryResult(False, 0, "不支持的通知渠道")
    return deliver_with_retries(sender, max_attempts=3)
