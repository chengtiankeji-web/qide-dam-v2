"""/v1/crm/quotes · 报价单 REST API + PDF 生成 + 发送"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.schemas.crm.quote import (
    QuoteCreate, QuoteOut, QuoteListOut, QuoteUpdate,
    QuoteStatusTransitionIn, QuoteSendIn, QuotePdfOut,
)
from app.services.crm import quotes_service

router = APIRouter()


@router.post("/", response_model=QuoteOut, status_code=http_status.HTTP_201_CREATED)
async def create_quote(
    payload: QuoteCreate,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> QuoteOut:
    quote = await quotes_service.create_quote(
        db,
        principal=principal,
        tenant_id=principal.tenant_id,
        deal_id=payload.deal_id,
        line_items=[item.model_dump() for item in payload.line_items],
        account_id=payload.account_id,
        contact_id=payload.contact_id,
        factory_slug=payload.factory_slug,
        discount_usd=payload.discount_usd,
        tax_usd=payload.tax_usd,
        shipping_usd=payload.shipping_usd,
        currency=payload.currency,
        validity_days=payload.validity_days,
        payment_terms=payload.payment_terms,
        delivery_terms=payload.delivery_terms,
        delivery_port=payload.delivery_port,
        estimated_lead_time_days=payload.estimated_lead_time_days,
        internal_notes=payload.internal_notes,
        customer_notes=payload.customer_notes,
    )
    await db.commit()
    return QuoteOut.model_validate(quote)


@router.get("/", response_model=QuoteListOut)
async def list_quotes(
    deal_id: Optional[uuid.UUID] = Query(None),
    account_id: Optional[uuid.UUID] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> QuoteListOut:
    rows = await quotes_service.list_quotes(
        db,
        principal=principal,
        tenant_id=principal.tenant_id,
        deal_id=deal_id,
        account_id=account_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return QuoteListOut(
        items=[QuoteOut.model_validate(r) for r in rows],
        total=len(rows), limit=limit, offset=offset,
    )


@router.get("/{quote_id}", response_model=QuoteOut)
async def get_quote(
    quote_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> QuoteOut:
    from app.models.crm.quote import Quote
    quote = await db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(404, "Quote not found")
    if not principal.is_platform_admin and quote.tenant_id != principal.tenant_id:
        raise HTTPException(403, "Forbidden")
    return QuoteOut.model_validate(quote)


@router.patch("/{quote_id}", response_model=QuoteOut)
async def update_quote(
    quote_id: uuid.UUID,
    payload: QuoteUpdate,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> QuoteOut:
    """编辑 quote · 仅 draft/revised 状态可改"""
    if not payload.line_items:
        raise HTTPException(400, "line_items required for now (full update later)")
    try:
        quote = await quotes_service.update_line_items(
            db,
            principal=principal,
            quote_id=quote_id,
            line_items=[item.model_dump() for item in payload.line_items],
            discount_usd=payload.discount_usd,
            tax_usd=payload.tax_usd,
            shipping_usd=payload.shipping_usd,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.commit()
    return QuoteOut.model_validate(quote)


@router.post("/{quote_id}/transition", response_model=QuoteOut)
async def transition_quote(
    quote_id: uuid.UUID,
    payload: QuoteStatusTransitionIn,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> QuoteOut:
    try:
        quote = await quotes_service.transition_status(
            db,
            principal=principal,
            quote_id=quote_id,
            new_status=payload.new_status,
            sent_to_email=payload.sent_to_email,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.commit()
    return QuoteOut.model_validate(quote)


@router.post("/{quote_id}/generate-pdf", response_model=QuotePdfOut)
async def generate_quote_pdf(
    quote_id: uuid.UUID,
    locale: str = Query("en", pattern="^(en|zh)$"),
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> QuotePdfOut:
    """生成 PDF + 上 R2 · 返回 1 小时 expiry signed URL"""
    try:
        quote, signed_url = await quotes_service.generate_pdf(
            db, principal=principal, quote_id=quote_id, locale=locale,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.commit()
    return QuotePdfOut(
        quote_id=quote.id,
        pdf_storage_key=quote.pdf_storage_key,
        signed_download_url=signed_url,
        expires_in_seconds=3600,
    )


@router.post("/{quote_id}/send", response_model=QuoteOut)
async def send_quote(
    quote_id: uuid.UUID,
    payload: QuoteSendIn,
    principal: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> QuoteOut:
    """生成 PDF + 发邮件给客户 + 改 status='sent'"""
    # 1. 生 PDF
    try:
        quote, signed_url = await quotes_service.generate_pdf(
            db, principal=principal, quote_id=quote_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    # 2. 发邮件（走 email_service · 复用 Resend）
    from app.services.crm import email_service
    subject = payload.subject or f"Quotation {quote.quote_number}"
    body_html = payload.body_html or _default_quote_email_template(quote, signed_url)
    try:
        await email_service.send_email(
            db,
            principal=principal,
            tenant_id=principal.tenant_id,
            to_emails=[payload.to_email],
            cc_emails=payload.cc_emails or [],
            subject=subject,
            html_body=body_html,
            related_quote_id=quote_id,
        )
    except Exception as e:
        # PDF 已生 · 邮件失败 · 不阻塞·返回 status 维持 draft + 报警
        raise HTTPException(502, f"Email send failed: {str(e)[:200]}")

    # 3. 改状态 draft → sent
    if quote.status == "draft":
        try:
            quote = await quotes_service.transition_status(
                db, principal=principal, quote_id=quote_id,
                new_status="sent", sent_to_email=payload.to_email,
            )
        except ValueError:
            pass  # 已 sent / 状态不合·不阻塞

    await db.commit()
    return QuoteOut.model_validate(quote)


def _default_quote_email_template(quote, pdf_url: str) -> str:
    """默认邮件 HTML 模板"""
    return f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px;">
      <h2>Quotation {quote.quote_number}</h2>
      <p>Dear customer,</p>
      <p>Please find attached our quotation for your inquiry.
      The total amount is <strong>${float(quote.total_usd):,.2f} {quote.currency}</strong>.</p>
      <p><a href="{pdf_url}"
            style="background:#1A3C5E;color:#fff;padding:10px 20px;
                   text-decoration:none;border-radius:4px;">
        Download Quotation PDF
      </a></p>
      <p>Validity: this quotation is valid until
        {quote.expires_at.strftime('%Y-%m-%d') if quote.expires_at else 'further notice'}.</p>
      <p>Please contact us if you have any questions.</p>
      <hr style="border:none;border-top:1px solid #eee;margin:30px 0;"/>
      <p style="font-size:11px;color:#888;">
        This email was sent from Qide Link Tech CRM ·
        Quote #{quote.quote_number}
      </p>
    </body></html>
    """
