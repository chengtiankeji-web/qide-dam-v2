"""Pydantic schemas for Quote"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class QuoteLineItem(BaseModel):
    sku_slug: str | None = None
    sku_name: str | None = None
    description: str | None = None
    qty: int = Field(..., ge=0)
    unit_price_usd: Decimal = Field(..., ge=0)
    line_total_usd: Decimal | None = None  # 服务端算
    hs_code: str | None = None
    weight_kg: Decimal | None = None


class QuoteCreate(BaseModel):
    deal_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    factory_slug: str | None = Field(None, max_length=64)
    line_items: list[QuoteLineItem] = Field(..., min_length=1)
    discount_usd: Decimal = Field(Decimal("0"), ge=0)
    tax_usd: Decimal = Field(Decimal("0"), ge=0)
    shipping_usd: Decimal = Field(Decimal("0"), ge=0)
    currency: str = Field("USD", max_length=8)
    validity_days: int = Field(30, ge=1, le=365)
    payment_terms: str | None = Field(None, max_length=256)
    delivery_terms: str | None = Field(
        None, pattern="^(EXW|FOB|CIF|CFR|DDP|DAP|FCA|CPT|CIP|DPU)$"
    )
    delivery_port: str | None = Field(None, max_length=128)
    estimated_lead_time_days: int | None = Field(None, ge=1)
    internal_notes: str | None = None
    customer_notes: str | None = None


class QuoteUpdate(BaseModel):
    line_items: list[QuoteLineItem] | None = None
    discount_usd: Decimal | None = None
    tax_usd: Decimal | None = None
    shipping_usd: Decimal | None = None
    payment_terms: str | None = None
    delivery_terms: str | None = None
    delivery_port: str | None = None
    estimated_lead_time_days: int | None = None
    internal_notes: str | None = None
    customer_notes: str | None = None


class QuoteStatusTransitionIn(BaseModel):
    new_status: str = Field(
        ..., pattern="^(sent|viewed|accepted|declined|expired|revised|cancelled)$"
    )
    sent_to_email: EmailStr | None = None


class QuoteSendIn(BaseModel):
    """POST /v1/crm/quotes/{id}/send · 同步生成 PDF + 发邮件 + 改状态"""
    to_email: EmailStr
    cc_emails: list[EmailStr] | None = None
    subject: str | None = None  # 默认: "Quotation {quote_number} from <factory>"
    body_html: str | None = None  # 默认模板


class QuoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    quote_number: str
    deal_id: uuid.UUID | None
    account_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    factory_slug: str | None

    line_items: list[dict]
    subtotal_usd: Decimal
    discount_usd: Decimal | None
    tax_usd: Decimal | None
    shipping_usd: Decimal | None
    total_usd: Decimal
    currency: str

    validity_days: int
    payment_terms: str | None
    delivery_terms: str | None
    delivery_port: str | None
    estimated_lead_time_days: int | None

    status: str
    sent_at: datetime | None
    sent_to_email: str | None
    viewed_at: datetime | None
    accepted_at: datetime | None
    declined_at: datetime | None
    expires_at: datetime | None

    pdf_storage_key: str | None
    pdf_generated_at: datetime | None

    owner_user_id: uuid.UUID | None
    internal_notes: str | None
    customer_notes: str | None

    created_at: datetime
    updated_at: datetime


class QuotePdfOut(BaseModel):
    """generate-pdf 响应"""
    quote_id: uuid.UUID
    pdf_storage_key: str
    signed_download_url: str
    expires_in_seconds: int


class QuoteListOut(BaseModel):
    items: list[QuoteOut]
    total: int
    limit: int
    offset: int
