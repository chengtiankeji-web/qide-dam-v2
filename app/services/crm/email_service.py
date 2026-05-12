"""email_service · Resend API 集成 · 发送 + tracking webhook 接收

Resend 简介：
  - https://resend.com · OpenAI-compatible style API
  - 月 $20 起·3000 emails 免费 tier · 5K $20 / 50K $80
  - 自带 open / click / bounce tracking webhook
  - 直接发 HTML + plain text + attachment
  - 多域名 sender 支持（祁德 / 青玄 / 工厂等 · 走 DNS 验证）

API:
  POST https://api.resend.com/emails
    headers: Authorization: Bearer re_xxxx
    body: {from, to, subject, html, attachments, headers}
  → {id: "abc-123", ...}

Webhook events:
  - email.sent / delivered / bounced / opened / clicked / complained / delivery_delayed
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.core.config import settings
from app.core.deps import Principal
from app.core.logging import get_logger
from app.models.crm.activity import CRMActivity
from app.services import audit_service
from app.services.audit_service import AuditAction

logger = get_logger(__name__)


RESEND_API_URL = "https://api.resend.com/emails"


async def send_email(
    db,
    *,
    principal: Principal,
    tenant_id: uuid.UUID,
    to_emails: list[str],
    subject: str,
    html_body: str,
    text_body: Optional[str] = None,
    cc_emails: Optional[list[str]] = None,
    bcc_emails: Optional[list[str]] = None,
    from_email: Optional[str] = None,  # 默认 settings.RESEND_FROM_EMAIL
    from_name: Optional[str] = None,
    reply_to: Optional[str] = None,
    attachments: Optional[list[dict]] = None,  # [{filename, content: bytes / b64, content_type}]
    # CRM 关联（写 activity / 回溯 lead / deal / quote）
    related_lead_id: Optional[uuid.UUID] = None,
    related_deal_id: Optional[uuid.UUID] = None,
    related_quote_id: Optional[uuid.UUID] = None,
    related_contact_id: Optional[uuid.UUID] = None,
    # 营销 unsubscribe link
    unsubscribe_url: Optional[str] = None,
    # tag for webhook 回溯
    custom_tags: Optional[dict] = None,
) -> dict:
    """发送邮件 + 写 activity timeline · 返回 Resend message_id"""
    if not settings.RESEND_API_KEY:
        # Dev / stub mode：log + 假返回（同 ai_service stub mode 模式）
        logger.warning(
            "email.stub_mode",
            reason="RESEND_API_KEY not set",
            to=to_emails, subject=subject,
        )
        return {
            "message_id": f"stub-{uuid.uuid4()}",
            "stub": True,
        }

    from_addr = from_email or settings.RESEND_FROM_EMAIL or "noreply@qidelinktech.com"
    from_display = (
        f"{from_name} <{from_addr}>" if from_name else from_addr
    )

    # 准备 payload
    payload = {
        "from": from_display,
        "to": to_emails,
        "subject": subject,
        "html": html_body,
    }
    if text_body:
        payload["text"] = text_body
    if cc_emails:
        payload["cc"] = cc_emails
    if bcc_emails:
        payload["bcc"] = bcc_emails
    if reply_to:
        payload["reply_to"] = reply_to
    if attachments:
        payload["attachments"] = [
            {
                "filename": a["filename"],
                "content": (
                    base64.b64encode(a["content"]).decode()
                    if isinstance(a["content"], bytes) else a["content"]
                ),
                "content_type": a.get("content_type", "application/octet-stream"),
            }
            for a in attachments
        ]
    # 自定义 headers + tags · webhook 回溯关联
    headers_obj = {}
    if unsubscribe_url:
        headers_obj["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        headers_obj["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    if headers_obj:
        payload["headers"] = headers_obj

    # tags · Resend 支持 · 用于 webhook 回溯
    tag_list = []
    if related_lead_id:
        tag_list.append({"name": "lead_id", "value": str(related_lead_id)})
    if related_deal_id:
        tag_list.append({"name": "deal_id", "value": str(related_deal_id)})
    if related_quote_id:
        tag_list.append({"name": "quote_id", "value": str(related_quote_id)})
    if related_contact_id:
        tag_list.append({"name": "contact_id", "value": str(related_contact_id)})
    if custom_tags:
        for k, v in custom_tags.items():
            tag_list.append({"name": k, "value": str(v)})
    if tag_list:
        payload["tags"] = tag_list

    # 发
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                RESEND_API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            message_id = data.get("id")

            logger.info(
                "email.sent",
                message_id=message_id, to=to_emails, subject=subject[:50],
            )
    except httpx.HTTPStatusError as e:
        logger.error("email.resend_error",
                    status_code=e.response.status_code,
                    body=e.response.text[:500])
        raise RuntimeError(f"Resend API error {e.response.status_code}: {e.response.text[:200]}")
    except httpx.RequestError as e:
        logger.error("email.network_error", error=str(e))
        raise RuntimeError(f"Resend network error: {e}")

    # 写 activity timeline（如有关联 entity）
    primary_entity_type = None
    primary_entity_id = None
    if related_quote_id:
        primary_entity_type, primary_entity_id = "quote", related_quote_id
    elif related_deal_id:
        primary_entity_type, primary_entity_id = "deal", related_deal_id
    elif related_lead_id:
        primary_entity_type, primary_entity_id = "lead", related_lead_id
    elif related_contact_id:
        primary_entity_type, primary_entity_id = "contact", related_contact_id

    if primary_entity_type:
        activity = CRMActivity(
            tenant_id=tenant_id,
            activity_type="email",
            entity_type=primary_entity_type,
            entity_id=primary_entity_id,
            subject=subject,
            description=f"Email sent to {', '.join(to_emails)}",
            performed_by_user_id=principal.user_id,
            email_message_id=message_id,
            email_from=from_addr,
            email_to=to_emails,
            email_subject=subject,
            email_body_preview=html_body[:500] if html_body else None,
        )
        db.add(activity)
        await db.flush()

    return {"message_id": message_id, "stub": False}


async def handle_resend_webhook(db, event: dict) -> None:
    """处理 Resend webhook

    Event types:
      email.sent / delivered / bounced / opened / clicked / complained

    每个事件查 email_message_id → 更新 crm_activities + 关联 quote/lead/deal
    """
    event_type = event.get("type")
    data = event.get("data", {})
    message_id = data.get("email_id") or data.get("id")
    if not message_id:
        logger.warning("resend_webhook.no_message_id", event=event)
        return

    # 查 activity
    from sqlalchemy import select, update as sql_update
    result = await db.execute(
        select(CRMActivity).where(CRMActivity.email_message_id == message_id)
    )
    activity = result.scalar_one_or_none()
    if not activity:
        logger.warning("resend_webhook.activity_not_found", message_id=message_id)
        return

    now = datetime.now(timezone.utc)

    if event_type == "email.opened":
        if not activity.email_opened_at:
            activity.email_opened_at = now
        # 如果关联 quote · 自动 transition 状态到 viewed
        if activity.entity_type == "quote":
            from app.models.crm.quote import Quote
            quote = await db.get(Quote, activity.entity_id)
            if quote and quote.status == "sent":
                quote.status = "viewed"
                quote.viewed_at = now
                await audit_service.log(
                    db, principal=None,  # webhook · 无 user
                    action=AuditAction.QUOTE_VIEWED,
                    target_kind="quote", target_id=quote.id,
                    payload={"via_email_open": True, "message_id": message_id},
                )

    elif event_type == "email.clicked":
        if not activity.email_clicked_at:
            activity.email_clicked_at = now
        # 自动写 click event 到 metadata
        click_url = data.get("click", {}).get("link")
        if click_url:
            md = activity.metadata or {}
            clicks = md.get("click_events", [])
            clicks.append({"url": click_url, "ts": now.isoformat()})
            md["click_events"] = clicks
            activity.metadata = md

    elif event_type == "email.bounced":
        md = activity.metadata or {}
        md["bounced_at"] = now.isoformat()
        md["bounce_reason"] = data.get("bounce", {}).get("message")
        activity.metadata = md
        # 标 contact bounced（避免再发）
        if activity.entity_type == "contact":
            from app.models.crm.contact import Contact
            contact = await db.get(Contact, activity.entity_id)
            if contact:
                contact.bounced = True

    elif event_type == "email.complained":
        # 用户标垃圾邮件 · contact 自动 unsubscribe
        if activity.entity_type == "contact":
            from app.models.crm.contact import Contact
            contact = await db.get(Contact, activity.entity_id)
            if contact:
                contact.opt_in_marketing = False
                contact.unsubscribed_at = now

    await db.flush()
    logger.info("resend_webhook.processed",
                event_type=event_type, activity_id=str(activity.id))
