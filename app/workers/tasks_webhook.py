"""HMAC-signed webhook delivery with exponential backoff retry."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from app.core.logging import get_logger
from app.models.webhook import WebhookDelivery, WebhookSubscription
from app.workers._db import session_scope
from app.workers.celery_app import celery_app

logger = get_logger("worker.webhook")

MAX_ATTEMPTS = 6  # 0, 30s, 2m, 10m, 1h, 6h
BACKOFF_SECONDS = [30, 120, 600, 3600, 21600]
MAX_FAILURES_BEFORE_SUSPEND = 20


def _sign(secret: str, body_bytes: bytes, ts: int | None = None) -> tuple[str, int]:
    ts = ts or int(time.time())
    raw = f"{ts}.".encode() + body_bytes
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}", ts


@celery_app.task(name="webhook.deliver", bind=True, max_retries=MAX_ATTEMPTS - 1)
def deliver(self, delivery_id: str) -> dict:
    with session_scope() as db:
        delivery = db.get(WebhookDelivery, uuid.UUID(delivery_id))
        if not delivery:
            return {"delivery_id": delivery_id, "status": "missing"}
        if delivery.status in ("succeeded", "dead"):
            return {"delivery_id": delivery_id, "status": "noop"}

        sub = db.get(WebhookSubscription, delivery.subscription_id)
        if not sub or not sub.is_active:
            delivery.status = "dead"
            delivery.error = "subscription inactive"
            db.add(delivery)
            return {"delivery_id": delivery_id, "status": "dead"}

        body = json.dumps(
            {
                "event": delivery.event_type,
                "delivery_id": str(delivery.id),
                "tenant_id": str(delivery.tenant_id),
                "payload": delivery.payload,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        sig_header, _ = _sign(sub.secret, body)

        delivery.attempt_count += 1
        delivery.last_attempt_at = datetime.now(timezone.utc)

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(
                    sub.target_url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Qide-Event": delivery.event_type,
                        "X-Qide-Signature": sig_header,
                        "User-Agent": "QideDAM-Webhook/2.0",
                    },
                )
            delivery.response_status = resp.status_code
            preview = resp.text[:512] if resp.text else None
            delivery.response_body = preview
            delivery.response_size = len(resp.content) if resp.content else 0

            if 200 <= resp.status_code < 300:
                delivery.status = "succeeded"
                sub.consecutive_failures = 0
                sub.last_delivered_at = datetime.now(timezone.utc)
                db.add_all([delivery, sub])
                logger.info("webhook.delivery.ok", delivery_id=delivery_id,
                            status=resp.status_code)
                return {"delivery_id": delivery_id, "status": "ok"}

            delivery.error = f"http {resp.status_code}"
        except Exception as e:  # noqa: BLE001
            delivery.error = f"{type(e).__name__}: {e}"

        sub.consecutive_failures += 1
        if sub.consecutive_failures >= MAX_FAILURES_BEFORE_SUSPEND:
            sub.is_active = False
            sub.suspended_at = datetime.now(timezone.utc)
            logger.warning("webhook.subscription.suspended",
                           subscription_id=str(sub.id),
                           failures=sub.consecutive_failures)

        if delivery.attempt_count >= MAX_ATTEMPTS:
            delivery.status = "dead"
            db.add_all([delivery, sub])
            return {"delivery_id": delivery_id, "status": "dead"}

        # Schedule next retry with backoff
        next_in = BACKOFF_SECONDS[
            min(delivery.attempt_count - 1, len(BACKOFF_SECONDS) - 1)
        ]
        delivery.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=next_in)
        delivery.status = "failed"
        db.add_all([delivery, sub])

    raise self.retry(countdown=next_in)
