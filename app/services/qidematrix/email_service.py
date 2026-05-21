"""Email outbox service · 队列 + 发送 + 5 重试 + 死信

发送 provider 优先级：
  1. Resend (RESEND_API_KEY · 推荐 · qidelinktech.cn 已验证域)
  2. SMTP (EMAIL_SMTP_HOST / PORT / USER / PASS · 兜底)
  3. Stub (无 provider · 日志记录但不真发 · 开发模式)
"""
from __future__ import annotations

import os
import smtplib
import uuid
from datetime import UTC, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import httpx
from sqlalchemy import select, update, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.qidematrix.pipeline import QmEmailOutbox
from app.services.qidematrix.email_templates import render_email

logger = get_logger("qm.email")


MAX_ATTEMPTS = 5
RESEND_API_URL = "https://api.resend.com/emails"


# ═════════════════════════════════════════════════════════════════════
# 1. 入队
# ═════════════════════════════════════════════════════════════════════

async def queue_email(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    template_key: str,
    locale: str,
    to_email: str,
    to_name: str | None = None,
    template_vars: dict[str, Any] | None = None,
    attachments: list[dict] | None = None,
    workspace_id: uuid.UUID | None = None,
    onboarding_id: uuid.UUID | None = None,
    diagnostic_id: uuid.UUID | None = None,
    related_event_id: uuid.UUID | None = None,
    send_after: datetime | None = None,
    from_email: str | None = None,
) -> QmEmailOutbox:
    """渲染模板 + 写入 outbox · 等 worker 发"""
    from_email = from_email or os.getenv(
        "QM_EMAIL_FROM", "no-reply@qidelinktech.cn"
    )
    rendered = render_email(
        template_key=template_key,
        locale=locale,
        template_vars=template_vars or {},
        from_email=from_email,
    )

    now = datetime.now(UTC)
    outbox = QmEmailOutbox(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        template_key=template_key,
        locale=locale,
        to_email=to_email,
        to_name=to_name,
        from_email=from_email,
        subject=rendered["subject"],
        body_text=rendered["body_text"],
        body_html=rendered["body_html"] or None,
        attachments=attachments or [],
        template_vars=template_vars or {},
        onboarding_id=onboarding_id,
        diagnostic_id=diagnostic_id,
        related_event_id=related_event_id,
        status="queued",
        send_after=send_after or now,
        attempts=0,
        max_attempts=MAX_ATTEMPTS,
        created_at=now,
        updated_at=now,
    )
    db.add(outbox)
    await db.flush()

    logger.info(
        "qm.email.queued",
        outbox_id=str(outbox.id),
        template_key=template_key,
        to=to_email,
    )
    return outbox


# ═════════════════════════════════════════════════════════════════════
# 2. claim · worker 拉一条 ready 的邮件
# ═════════════════════════════════════════════════════════════════════

async def claim_next_ready(
    db: AsyncSession, *, limit: int = 5
) -> list[QmEmailOutbox]:
    """拉一批待发 · 原子翻 status='sending'"""
    sql = text("""
        WITH ready AS (
            SELECT id FROM qm_email_outbox
            WHERE status = 'queued'
              AND send_after <= NOW()
              AND attempts < max_attempts
            ORDER BY send_after ASC
            FOR UPDATE SKIP LOCKED
            LIMIT :limit
        )
        UPDATE qm_email_outbox e
        SET
            status = 'sending',
            attempts = e.attempts + 1,
            last_attempt_at = NOW()
        FROM ready
        WHERE e.id = ready.id
        RETURNING e.id
    """)
    result = await db.execute(sql, {"limit": limit})
    ids = [row[0] for row in result.fetchall()]
    if not ids:
        return []

    rows = await db.execute(
        select(QmEmailOutbox).where(QmEmailOutbox.id.in_(ids))
    )
    return list(rows.scalars().all())


# ═════════════════════════════════════════════════════════════════════
# 3. send · 真发 · provider 抉择
# ═════════════════════════════════════════════════════════════════════

def _send_via_resend(outbox: QmEmailOutbox) -> tuple[bool, str | None, str | None]:
    """走 Resend HTTP API · 返回 (ok, provider_msg_id, error)"""
    api_key = os.getenv("RESEND_API_KEY", "")
    if not api_key:
        return False, None, "RESEND_API_KEY missing"

    payload: dict[str, Any] = {
        "from": outbox.from_email,
        "to": [outbox.to_email],
        "subject": outbox.subject,
    }
    if outbox.body_html:
        payload["html"] = outbox.body_html
    if outbox.body_text:
        payload["text"] = outbox.body_text
    if outbox.cc_emails:
        payload["cc"] = outbox.cc_emails
    if outbox.bcc_emails:
        payload["bcc"] = outbox.bcc_emails
    if outbox.reply_to:
        payload["reply_to"] = outbox.reply_to

    try:
        resp = httpx.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30.0,
        )
        if resp.status_code >= 400:
            return False, None, f"resend {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
        return True, data.get("id"), None
    except Exception as exc:
        return False, None, f"resend exception: {exc}"


def _send_via_smtp(outbox: QmEmailOutbox) -> tuple[bool, str | None, str | None]:
    """走 SMTP · 返回 (ok, provider_msg_id, error)"""
    host = os.getenv("EMAIL_SMTP_HOST", "")
    port = int(os.getenv("EMAIL_SMTP_PORT", "587"))
    user = os.getenv("EMAIL_SMTP_USER", "")
    password = os.getenv("EMAIL_SMTP_PASS", "")
    if not host or not user:
        return False, None, "SMTP not configured"

    msg = MIMEMultipart("alternative")
    msg["From"] = outbox.from_email
    msg["To"] = outbox.to_email
    msg["Subject"] = outbox.subject
    if outbox.body_text:
        msg.attach(MIMEText(outbox.body_text, "plain", "utf-8"))
    if outbox.body_html:
        msg.attach(MIMEText(outbox.body_html, "html", "utf-8"))

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
        return True, f"smtp-{uuid.uuid4().hex[:8]}", None
    except Exception as exc:
        return False, None, f"smtp exception: {exc}"


def send_email_sync(outbox: QmEmailOutbox) -> tuple[bool, str | None, str | None]:
    """真发 · provider 优先级 Resend → SMTP → Stub"""
    if os.getenv("RESEND_API_KEY"):
        ok, mid, err = _send_via_resend(outbox)
        if ok:
            return True, mid, None
        logger.warning("qm.email.resend_failed", err=err)
        # 不切到 SMTP · 因为模板 + 域已对齐 Resend · 留个 fallback flag

    if os.getenv("EMAIL_SMTP_HOST"):
        ok, mid, err = _send_via_smtp(outbox)
        if ok:
            return True, mid, None
        return False, None, err

    # Stub mode（开发用 · 不真发）
    logger.info(
        "qm.email.stub_send",
        to=outbox.to_email,
        subject=outbox.subject,
        outbox_id=str(outbox.id),
    )
    return True, f"stub-{uuid.uuid4().hex[:8]}", None


async def mark_sent(
    db: AsyncSession, *, outbox_id: uuid.UUID, provider_msg_id: str | None
) -> None:
    await db.execute(
        update(QmEmailOutbox)
        .where(QmEmailOutbox.id == outbox_id)
        .values(
            status="sent",
            sent_at=datetime.now(UTC),
            provider=("resend" if os.getenv("RESEND_API_KEY") else
                      "smtp" if os.getenv("EMAIL_SMTP_HOST") else "stub"),
            provider_msg_id=provider_msg_id,
        )
    )


async def mark_failed(
    db: AsyncSession, *, outbox_id: uuid.UUID, error: str
) -> None:
    """失败 → 如果未达 max_attempts 翻回 queued · 否则 failed"""
    await db.execute(
        text("""
            UPDATE qm_email_outbox
            SET
                status = CASE
                    WHEN attempts >= max_attempts THEN 'failed'
                    ELSE 'queued'
                END,
                last_error = :error,
                -- 指数退避：next attempt 在 attempts^2 分钟后
                send_after = CASE
                    WHEN attempts < max_attempts
                    THEN NOW() + (attempts * attempts || ' minutes')::interval
                    ELSE send_after
                END
            WHERE id = :outbox_id
        """),
        {"outbox_id": outbox_id, "error": (error or "")[:2000]},
    )


# ═════════════════════════════════════════════════════════════════════
# 4. 查询
# ═════════════════════════════════════════════════════════════════════

async def get_outbox(
    db: AsyncSession, *, outbox_id: uuid.UUID
) -> QmEmailOutbox | None:
    result = await db.execute(
        select(QmEmailOutbox).where(QmEmailOutbox.id == outbox_id)
    )
    return result.scalar_one_or_none()


async def list_outbox(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[QmEmailOutbox]:
    stmt = select(QmEmailOutbox).order_by(QmEmailOutbox.created_at.desc())
    if tenant_id:
        stmt = stmt.where(QmEmailOutbox.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(QmEmailOutbox.status == status)
    stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())
