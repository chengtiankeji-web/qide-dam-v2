"""Pydantic schemas for lead 资源·API I/O"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# ════════════════════════════════════════════════════════════
# Input schemas
# ════════════════════════════════════════════════════════════

class LeadCreate(BaseModel):
    """POST /v1/crm/leads"""
    tenant_id: uuid.UUID | None = None  # null = principal.tenant_id
    factory_slug: str = Field(..., min_length=1, max_length=64)
    source: str = Field(..., max_length=32,
                        description="linkedin/fb/ig/tiktok/whatsapp/email/cmh-form/share-link/cold/referral/other")
    inquiry_text: str = Field(..., min_length=1)
    project_id: uuid.UUID | None = None
    inquiry_language: str | None = Field(None, max_length=8)
    inquiry_attachments: list[dict] | None = None

    # 联系人
    contact_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    contact_name: str | None = Field(None, max_length=256)
    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(None, max_length=64)
    contact_company: str | None = Field(None, max_length=256)
    contact_country: str | None = Field(None, max_length=64)
    contact_role: str | None = Field(None, max_length=256)

    # 来源关联
    source_inbox_id: uuid.UUID | None = None
    source_share_link_id: uuid.UUID | None = None
    source_campaign: str | None = Field(None, max_length=128)
    source_url: str | None = None


class LeadAssignIn(BaseModel):
    """POST /v1/crm/leads/{id}/assign"""
    assignee_user_id: uuid.UUID


class LeadTransitionIn(BaseModel):
    """POST /v1/crm/leads/{id}/transition"""
    new_status: str = Field(..., pattern="^(new|contacted|qualified|unqualified|nurturing|converted|lost|spam|archived)$")
    note: str | None = None
    lost_reason: str | None = Field(None, max_length=128)
    lost_competitor: str | None = Field(None, max_length=256)


class LeadOverrideClassificationIn(BaseModel):
    """POST /v1/crm/leads/{id}/classify/override"""
    classification: str = Field(..., pattern="^[ABCD]$")
    reason: str = Field(..., min_length=1, max_length=500)


class LeadConvertIn(BaseModel):
    """POST /v1/crm/leads/{id}/convert"""
    deal_name: str = Field(..., min_length=1, max_length=256)
    estimated_value_usd: float | None = Field(None, ge=0)
    probability_pct: int = Field(50, ge=0, le=100)
    expected_close_date: str | None = None  # ISO date
    related_sku_slugs: list[str] | None = None


# ════════════════════════════════════════════════════════════
# Output schemas
# ════════════════════════════════════════════════════════════

class LeadOut(BaseModel):
    """LeadOut · 完整返回"""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    factory_slug: str
    project_id: uuid.UUID | None
    source: str
    source_campaign: str | None
    source_url: str | None

    contact_id: uuid.UUID | None
    account_id: uuid.UUID | None
    contact_name: str | None
    contact_email: str | None
    contact_phone: str | None
    contact_company: str | None
    contact_country: str | None
    contact_role: str | None

    inquiry_text: str
    inquiry_attachments: list[dict] | None
    inquiry_language: str | None

    # 6 要素
    has_quantity: bool
    has_budget: bool
    has_timeline: bool
    has_specification: bool
    has_decision_role: bool
    has_company_info: bool
    six_factor_score: int
    six_factor_breakdown: dict | None
    classification: str | None
    classification_overridden: bool

    # 状态
    status: str
    assigned_user_id: uuid.UUID | None
    assigned_at: datetime | None
    first_contact_at: datetime | None
    last_activity_at: datetime | None
    qualified_at: datetime | None
    converted_to_deal_id: uuid.UUID | None
    converted_at: datetime | None
    lost_at: datetime | None
    lost_reason: str | None
    lost_competitor: str | None

    # AI
    ai_intent_summary: str | None
    ai_suggested_reply: str | None
    ai_competitors_mentioned: list[str] | None
    ai_translated_zh: str | None
    ai_urgency_score: float | None
    ai_quality_score: float | None

    tags: list[str] | None
    notes: str | None

    created_at: datetime
    updated_at: datetime


class LeadListOut(BaseModel):
    items: list[LeadOut]
    total: int
    limit: int
    offset: int


class LeadDealOut(BaseModel):
    """convert 响应·返 lead + 新建 deal 概要"""
    lead: LeadOut
    deal_id: uuid.UUID
    deal_name: str
