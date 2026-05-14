"""quotes_service · 报价单 CRUD + line_items 计算 + 状态机 + PDF 生成"""
from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal
from app.core.logging import get_logger
from app.models.crm.deal import Deal
from app.models.crm.quote import Quote
from app.services import audit_service, storage
from app.services.audit_service import AuditAction

logger = get_logger(__name__)


# ────── 状态机 ──────
ALLOWED_QUOTE_TRANSITIONS = {
    "draft":    {"sent", "cancelled"},
    "sent":     {"viewed", "accepted", "declined", "expired", "revised", "cancelled"},
    "viewed":   {"accepted", "declined", "expired", "revised"},
    "accepted": {"cancelled"},  # 已 accept 仍可 cancel（极少）
    "declined": {"revised", "cancelled"},
    "expired":  {"revised", "cancelled"},
    "revised":  {"sent", "cancelled"},
    "cancelled": set(),  # 终态
}


def _calculate_totals(line_items: list[dict],
                      discount_usd: Decimal = Decimal("0"),
                      tax_usd: Decimal = Decimal("0"),
                      shipping_usd: Decimal = Decimal("0")) -> dict:
    """从 line_items 计算 subtotal / total"""
    subtotal = Decimal("0")
    for item in line_items:
        qty = Decimal(str(item.get("qty", 0) or 0))
        unit = Decimal(str(item.get("unit_price_usd", 0) or 0))
        line_total = qty * unit
        item["line_total_usd"] = float(line_total)  # 回写每行 total
        subtotal += line_total

    total = subtotal - Decimal(str(discount_usd)) + Decimal(str(tax_usd)) + Decimal(str(shipping_usd))
    return {
        "subtotal_usd": subtotal,
        "total_usd": total,
        "line_items": line_items,
    }


async def _generate_quote_number(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    """生成 quote_number · Q-2026-0001 格式 · 跨 tenant 独立编号"""
    year = datetime.now(timezone.utc).year
    prefix = f"Q-{year}-"
    q = (
        select(func.count(Quote.id))
        .where(Quote.tenant_id == tenant_id, Quote.quote_number.like(f"{prefix}%"))
    )
    count = (await db.execute(q)).scalar() or 0
    return f"{prefix}{count + 1:04d}"


async def create_quote(
    db: AsyncSession,
    *,
    principal: Principal,
    tenant_id: uuid.UUID,
    deal_id: uuid.UUID | None,
    line_items: list[dict],
    account_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
    factory_slug: str | None = None,
    discount_usd: Decimal = Decimal("0"),
    tax_usd: Decimal = Decimal("0"),
    shipping_usd: Decimal = Decimal("0"),
    currency: str = "USD",
    validity_days: int = 30,
    payment_terms: str | None = None,
    delivery_terms: str | None = None,  # Incoterms FOB/CIF/etc.
    delivery_port: str | None = None,
    estimated_lead_time_days: int | None = None,
    internal_notes: str | None = None,
    customer_notes: str | None = None,
) -> Quote:
    """创建报价单·自动算 subtotal/total"""
    # 自动 deal_id → account_id + contact_id + factory_slug（如未传）
    if deal_id and not (account_id or contact_id or factory_slug):
        deal = await db.get(Deal, deal_id)
        if deal:
            account_id = account_id or deal.account_id
            contact_id = contact_id or deal.primary_contact_id
            factory_slug = factory_slug or deal.factory_slug

    quote_number = await _generate_quote_number(db, tenant_id)

    # CRM ↔ DAM 整合·自动关联 master 图（v7 MVP）
    try:
        from app.services.crm import dam_integration_service
        line_items = await dam_integration_service.link_quote_items_to_dam_assets(
            db, tenant_id=tenant_id, line_items=line_items, factory_slug=factory_slug,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("dam_integration.failed", error=str(e)[:200])

    totals = _calculate_totals(line_items, discount_usd, tax_usd, shipping_usd)
    expires_at = datetime.now(timezone.utc) + timedelta(days=validity_days)

    quote = Quote(
        tenant_id=tenant_id,
        quote_number=quote_number,
        deal_id=deal_id,
        account_id=account_id,
        contact_id=contact_id,
        factory_slug=factory_slug,
        line_items=totals["line_items"],
        subtotal_usd=totals["subtotal_usd"],
        discount_usd=discount_usd,
        tax_usd=tax_usd,
        shipping_usd=shipping_usd,
        total_usd=totals["total_usd"],
        currency=currency,
        validity_days=validity_days,
        payment_terms=payment_terms,
        delivery_terms=delivery_terms,
        delivery_port=delivery_port,
        estimated_lead_time_days=estimated_lead_time_days,
        expires_at=expires_at,
        owner_user_id=principal.user_id,
        created_by_user_id=principal.user_id,
        internal_notes=internal_notes,
        customer_notes=customer_notes,
        status="draft",
    )
    db.add(quote)
    await db.flush()

    await audit_service.log(
        db, principal=principal,
        action=AuditAction.QUOTE_CREATED,
        target_kind="quote", target_id=quote.id,
        payload={
            "quote_number": quote_number,
            "total_usd": float(totals["total_usd"]),
            "deal_id": str(deal_id) if deal_id else None,
        },
    )
    return quote


async def update_line_items(
    db: AsyncSession,
    *,
    principal: Principal,
    quote_id: uuid.UUID,
    line_items: list[dict],
    discount_usd: Decimal | None = None,
    tax_usd: Decimal | None = None,
    shipping_usd: Decimal | None = None,
) -> Quote:
    """改 line_items + 重算 totals · 仅 draft / revised 状态可改"""
    quote = await db.get(Quote, quote_id)
    if not quote:
        raise ValueError(f"Quote {quote_id} not found")
    if quote.status not in ("draft", "revised"):
        raise ValueError(
            f"Cannot edit quote in status '{quote.status}' · only draft/revised allowed"
        )

    d = discount_usd if discount_usd is not None else quote.discount_usd
    t = tax_usd if tax_usd is not None else quote.tax_usd
    s = shipping_usd if shipping_usd is not None else quote.shipping_usd

    totals = _calculate_totals(line_items, d, t, s)
    quote.line_items = totals["line_items"]
    quote.subtotal_usd = totals["subtotal_usd"]
    quote.discount_usd = Decimal(str(d))
    quote.tax_usd = Decimal(str(t))
    quote.shipping_usd = Decimal(str(s))
    quote.total_usd = totals["total_usd"]

    # 修改后 PDF 失效 · 清掉缓存
    quote.pdf_storage_key = None
    quote.pdf_generated_at = None

    await db.flush()
    return quote


async def transition_status(
    db: AsyncSession,
    *,
    principal: Principal,
    quote_id: uuid.UUID,
    new_status: str,
    sent_to_email: str | None = None,
) -> Quote:
    """报价状态机"""
    quote = await db.get(Quote, quote_id)
    if not quote:
        raise ValueError(f"Quote {quote_id} not found")

    current = quote.status
    allowed = ALLOWED_QUOTE_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        raise ValueError(
            f"Cannot transition from '{current}' to '{new_status}' · allowed: {sorted(allowed)}"
        )

    now = datetime.now(timezone.utc)
    quote.status = new_status

    action_map = {
        "sent": AuditAction.QUOTE_SENT,
        "viewed": AuditAction.QUOTE_VIEWED,
        "accepted": AuditAction.QUOTE_ACCEPTED,
        "declined": AuditAction.QUOTE_DECLINED,
    }

    if new_status == "sent":
        quote.sent_at = now
        if sent_to_email:
            quote.sent_to_email = sent_to_email
    elif new_status == "viewed":
        quote.viewed_at = now
    elif new_status == "accepted":
        quote.accepted_at = now
    elif new_status == "declined":
        quote.declined_at = now

    if new_status in action_map:
        await audit_service.log(
            db, principal=principal,
            action=action_map[new_status],
            target_kind="quote", target_id=quote.id,
            payload={
                "from": current, "to": new_status,
                "total_usd": float(quote.total_usd),
                "sent_to_email": sent_to_email,
            },
        )

    await db.flush()
    return quote


async def generate_pdf(
    db: AsyncSession,
    *,
    principal: Principal,
    quote_id: uuid.UUID,
    locale: str = "en",  # en/zh · 双语 PDF v7.1 加
) -> tuple[Quote, str]:
    """生成 quote PDF 写到 R2 · 返回 (quote, signed_url)

    PDF 模板（v7 MVP 极简版·v7.1 让美工 + 小龙做品牌定制）：
      - Header: 工厂 logo + 公司名 + quote_number + date
      - Bill to: account + contact
      - Line items 表格
      - Totals 块
      - Payment terms + Incoterms
      - Notes
      - Footer: validity + 联系信息
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    quote = await db.get(Quote, quote_id)
    if not quote:
        raise ValueError(f"Quote {quote_id} not found")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title=f"Quotation {quote.quote_number}",
    )
    styles = getSampleStyleSheet()
    elements = []

    # Header
    elements.append(Paragraph(
        f"<b>QUOTATION</b><br/>{quote.quote_number}",
        styles["Title"],
    ))
    elements.append(Paragraph(
        f"Date: {quote.created_at.strftime('%Y-%m-%d')}",
        styles["Normal"],
    ))
    elements.append(Spacer(1, 0.5 * cm))

    # Bill to（如有 account / contact）
    if quote.account_id or quote.contact_id:
        from app.models.crm.account import Account
        from app.models.crm.contact import Contact
        bill_to_lines = ["<b>Bill To:</b>"]
        if quote.account_id:
            acc = await db.get(Account, quote.account_id)
            if acc:
                bill_to_lines.append(acc.display_name or acc.legal_name or "—")
                if acc.country:
                    bill_to_lines.append(acc.country)
        if quote.contact_id:
            c = await db.get(Contact, quote.contact_id)
            if c:
                bill_to_lines.append(f"Attn: {c.full_name}")
                if c.email:
                    bill_to_lines.append(c.email)
        elements.append(Paragraph("<br/>".join(bill_to_lines), styles["Normal"]))
        elements.append(Spacer(1, 0.5 * cm))

    # Line items table
    data = [["SKU", "Description", "Qty", "Unit Price", "Total"]]
    for item in quote.line_items or []:
        data.append([
            item.get("sku_slug", "") or "",
            item.get("description", "") or item.get("sku_name", "") or "",
            str(item.get("qty", "")),
            f"${item.get('unit_price_usd', 0):.2f}",
            f"${item.get('line_total_usd', 0):.2f}",
        ])
    table = Table(data, colWidths=[3 * cm, 7 * cm, 1.5 * cm, 2.5 * cm, 2.5 * cm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 0.5 * cm))

    # Totals
    totals_data = [
        ["Subtotal", f"${float(quote.subtotal_usd or 0):.2f}"],
    ]
    if quote.discount_usd and float(quote.discount_usd) > 0:
        totals_data.append(["Discount", f"-${float(quote.discount_usd):.2f}"])
    if quote.tax_usd and float(quote.tax_usd) > 0:
        totals_data.append(["Tax", f"${float(quote.tax_usd):.2f}"])
    if quote.shipping_usd and float(quote.shipping_usd) > 0:
        totals_data.append(["Shipping", f"${float(quote.shipping_usd):.2f}"])
    totals_data.append(["TOTAL", f"${float(quote.total_usd or 0):.2f} {quote.currency}"])

    totals_table = Table(totals_data, colWidths=[12 * cm, 4 * cm])
    totals_table.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, -1), (-1, -1), 11),
        ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black),
        ("TOPPADDING", (0, -1), (-1, -1), 6),
    ]))
    elements.append(totals_table)
    elements.append(Spacer(1, 0.8 * cm))

    # Terms
    terms_lines = []
    if quote.payment_terms:
        terms_lines.append(f"<b>Payment Terms:</b> {quote.payment_terms}")
    if quote.delivery_terms:
        port = f" {quote.delivery_port}" if quote.delivery_port else ""
        terms_lines.append(f"<b>Delivery Terms:</b> {quote.delivery_terms}{port} (Incoterms 2020)")
    if quote.estimated_lead_time_days:
        terms_lines.append(f"<b>Lead Time:</b> {quote.estimated_lead_time_days} days")
    if quote.expires_at:
        terms_lines.append(f"<b>Valid Until:</b> {quote.expires_at.strftime('%Y-%m-%d')}")
    if terms_lines:
        elements.append(Paragraph("<br/>".join(terms_lines), styles["Normal"]))
        elements.append(Spacer(1, 0.5 * cm))

    # Customer notes
    if quote.customer_notes:
        elements.append(Paragraph(f"<b>Notes:</b><br/>{quote.customer_notes}", styles["Normal"]))

    doc.build(elements)
    pdf_bytes = buf.getvalue()
    buf.close()

    # 写到 R2
    # v3 P1.3 phase 5+ (2026-05-14) SonarQube 真 bug 修复：
    # storage.put_object 是 sync (def · 非 async) + 所有参数 keyword-only
    # 之前 await + positional 任何调用 100% TypeError + RuntimeWarning
    storage_key = f"quotes/{quote.tenant_id}/{quote.id}.pdf"
    storage.put_object(
        storage_key=storage_key,
        body=pdf_bytes,
        content_type="application/pdf",
    )

    quote.pdf_storage_key = storage_key
    quote.pdf_generated_at = datetime.now(timezone.utc)
    await db.flush()

    # Signed URL（1 小时 expiry · 客户下载用）
    signed_url = storage.get_presigned_download_url(storage_key, expires_in=3600)

    return quote, signed_url


async def list_quotes(
    db: AsyncSession,
    *,
    principal: Principal,
    tenant_id: uuid.UUID,
    deal_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Quote]:
    q = select(Quote).where(Quote.tenant_id == tenant_id)
    if deal_id:
        q = q.where(Quote.deal_id == deal_id)
    if account_id:
        q = q.where(Quote.account_id == account_id)
    if status:
        q = q.where(Quote.status == status)
    q = q.order_by(Quote.created_at.desc()).limit(limit).offset(offset)
    return list((await db.execute(q)).scalars().all())
