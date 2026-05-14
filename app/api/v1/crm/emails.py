"""/v1/crm/emails · 邮件发送 + Resend webhook 接收"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.services.crm import email_service

router = APIRouter()


class SendEmailIn(BaseModel):
    to_emails: list[EmailStr] = Field(..., min_length=1)
    subject: str = Field(..., min_length=1, max_length=512)
    html_body: str = Field(..., min_length=1)
    text_body: str | None = None
    cc_emails: list[EmailStr] | None = None
    bcc_emails: list[EmailStr] | None = None
    from_email: EmailStr | None = None
    from_name: str | None = None
    reply_to: EmailStr | None = None
    related_lead_id: uuid.UUID | None = None
    related_deal_id: uuid.UUID | None = None
    related_quote_id: uuid.UUID | None = None
    related_contact_id: uuid.UUID | None = None
    unsubscribe_url: str | None = None


class SendEmailOut(BaseModel):
    message_id: str
    stub: bool = False


@router.post("/send", response_model=SendEmailOut, status_code=http_status.HTTP_202_ACCEPTED)
async def send_email(
    payload: SendEmailIn,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> SendEmailOut:
    """发送邮件 · 写 activity timeline + 关联 lead/deal/quote/contact"""
    try:
        result = await email_service.send_email(
            db,
            principal=principal,
            tenant_id=principal.tenant_id,
            to_emails=payload.to_emails,
            cc_emails=payload.cc_emails,
            bcc_emails=payload.bcc_emails,
            subject=payload.subject,
            html_body=payload.html_body,
            text_body=payload.text_body,
            from_email=payload.from_email,
            from_name=payload.from_name,
            reply_to=payload.reply_to,
            related_lead_id=payload.related_lead_id,
            related_deal_id=payload.related_deal_id,
            related_quote_id=payload.related_quote_id,
            related_contact_id=payload.related_contact_id,
            unsubscribe_url=payload.unsubscribe_url,
        )
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    await db.commit()
    return SendEmailOut(**result)


# ════════════════════════════════════════════════════════════
# Resend webhook · 公开端点（不需 auth · 但需验签）
# ════════════════════════════════════════════════════════════

@router.post("/webhooks/resend", status_code=http_status.HTTP_204_NO_CONTENT)
async def resend_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Resend webhook 接收 · event 类型：
    email.sent / delivered / bounced / opened / clicked / complained

    安全：通过 Svix 签名验证（Resend 用 Svix · header X-Webhook-Signature）
    生产前必须实现验签 · 当前 v7 MVP 信任 IP allowlist（Cloudflare 配）
    """
    body = await request.body()
    # TODO v7.1: Svix signature verification
    # svix_sig = request.headers.get("svix-signature")
    # svix_id = request.headers.get("svix-id")
    # svix_timestamp = request.headers.get("svix-timestamp")
    # verify(svix_sig, svix_id, svix_timestamp, body)

    import json
    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    await email_service.handle_resend_webhook(db, event)
    await db.commit()
