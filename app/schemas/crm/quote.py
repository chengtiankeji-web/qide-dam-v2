"""Pydantic schemas for Quote"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, ConfigDict


class QuoteLineItem(BaseModel):
    sku_slug: Optional[str] = None
    sku_name: Optional[str] = None
    description: Optional[str] = None
    qty: int = Field(..., ge=0)
    unit_price_usd: Decimal = Field(..., ge=0)
    line_total_usd: Optional[Decimal] = None  # 服务端算
    hs_code: Optional[str] = None
    weight_kg: Optional[Decimal] = None


class QuoteCreate(BaseModel):
    deal_id: Optional[uuid.UUID] = None
    account_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    factory_slug: Optional[str] = Field(None, max_length=64)
    line_items: list[QuoteLineItem] = Field(..., min_length=1)
    discount_usd: Decimal = Field(Decimal("0"), ge=0)
    tax_usd: Decimal = Field(Decimal("0"), ge=0)
    shipping_usd: Decimal = Field(Decimal("0"), ge=0)
    currency: str = Field("USD", max_length=8)
    validity_days: int = Field(30, ge=1, le=365)
    payment_terms: Optional[str] = Field(None, max_length=256)
    delivery_terms: Optional[str] = Field(
        None, pattern="^(EXW|FOB|CIF|CFR|DDP|DAP|FCA|CPT|CIP|DPU)$"
    )
    delivery_port: Optional[str] = Field(None, max_length=128)
    estimated_lead_time_days: Optional[int] = Field(None, ge=1)
    internal_notes: Optional[str] = None
    customer_notes: Optional[str] = None


class QuoteUpdate(BaseModel):
    line_items: Optional[list[QuoteLineItem]] = None
    discount_usd: Optional[Decimal] = None
    tax_usd: Optional[Decimal] = None
    shipping_usd: Optional[Decimal] = None
    payment_terms: Optional[str] = None
    delivery_terms: Optional[str] = None
    delivery_port: Optional[str] = None
    estimated_lead_time_days: Optional[int] = None
    internal_notes: Optional[str] = None
    customer_notes: Optional[str] = None


class QuoteStatusTransitionIn(BaseModel):
    new_status: str = Field(
        ..., pattern="^(sent|viewed|accepted|declined|expired|revised|cancelled)$"
    )
    sent_to_email: Optional[EmailStr] = None


class QuoteSendIn(BaseModel):
    """POST /v1/crm/quotes/{id}/send · 同步生成 PDF + 发邮件 + 改状态"""
    to_email: EmailStr
    cc_emails: Optional[list[EmailStr]] = None
    subject: Optional[str] = None  # 默认: "Quotation {quote_number} from <factory>"
    body_html: Optional[str] = None  # 默认模板


class QuoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    quote_number: str
    deal_id: Optional[uuid.UUID]
    account_id: Optional[uuid.UUID]
    contact_id: Optional[uuid.UUID]
    factory_slug: Optional[str]

    line_items: list[dict]
    subtotal_usd: Decimal
    discount_usd: Optional[Decimal]
    tax_usd: Optional[Decimal]
    shipping_usd: Optional[Decimal]
    total_usd: Decimal
    currency: str

    validity_days: int
    payment_terms: Optional[str]
    delivery_terms: Optional[str]
    delivery_port: Optional[str]
    estimated_lead_time_days: Optional[int]

    status: str
    sent_at: Optional[datetime]
    sent_to_email: Optional[str]
    viewed_at: Optional[datetime]
    accepted_at: Optional[datetime]
    declined_at: Optional[datetime]
    expires_at: Optional[datetime]

    pdf_storage_key: Optional[str]
    pdf_generated_at: Optional[datetime]

    owner_user_id: Optional[uuid.UUID]
    internal_notes: Optional[str]
    customer_notes: Optional[str]

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
