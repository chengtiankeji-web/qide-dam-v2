"""Pydantic schemas for lead 资源·API I/O"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, ConfigDict


# ════════════════════════════════════════════════════════════
# Input schemas
# ════════════════════════════════════════════════════════════

class LeadCreate(BaseModel):
    """POST /v1/crm/leads"""
    tenant_id: Optional[uuid.UUID] = None  # null = principal.tenant_id
    factory_slug: str = Field(..., min_length=1, max_length=64)
    source: str = Field(..., max_length=32,
                        description="linkedin/fb/ig/tiktok/whatsapp/email/cmh-form/share-link/cold/referral/other")
    inquiry_text: str = Field(..., min_length=1)
    project_id: Optional[uuid.UUID] = None
    inquiry_language: Optional[str] = Field(None, max_length=8)
    inquiry_attachments: Optional[list[dict]] = None

    # 联系人
    contact_id: Optional[uuid.UUID] = None
    account_id: Optional[uuid.UUID] = None
    contact_name: Optional[str] = Field(None, max_length=256)
    contact_email: Optional[EmailStr] = None
    contact_phone: Optional[str] = Field(None, max_length=64)
    contact_company: Optional[str] = Field(None, max_length=256)
    contact_country: Optional[str] = Field(None, max_length=64)
    contact_role: Optional[str] = Field(None, max_length=256)

    # 来源关联
    source_inbox_id: Optional[uuid.UUID] = None
    source_share_link_id: Optional[uuid.UUID] = None
    source_campaign: Optional[str] = Field(None, max_length=128)
    source_url: Optional[str] = None


class LeadAssignIn(BaseModel):
    """POST /v1/crm/leads/{id}/assign"""
    assignee_user_id: uuid.UUID


class LeadTransitionIn(BaseModel):
    """POST /v1/crm/leads/{id}/transition"""
    new_status: str = Field(..., pattern="^(new|contacted|qualified|unqualified|nurturing|converted|lost|spam|archived)$")
    note: Optional[str] = None
    lost_reason: Optional[str] = Field(None, max_length=128)
    lost_competitor: Optional[str] = Field(None, max_length=256)


class LeadOverrideClassificationIn(BaseModel):
    """POST /v1/crm/leads/{id}/classify/override"""
    classification: str = Field(..., pattern="^[ABCD]$")
    reason: str = Field(..., min_length=1, max_length=500)


class LeadConvertIn(BaseModel):
    """POST /v1/crm/leads/{id}/convert"""
    deal_name: str = Field(..., min_length=1, max_length=256)
    estimated_value_usd: Optional[float] = Field(None, ge=0)
    probability_pct: int = Field(50, ge=0, le=100)
    expected_close_date: Optional[str] = None  # ISO date
    related_sku_slugs: Optional[list[str]] = None


# ════════════════════════════════════════════════════════════
# Output schemas
# ════════════════════════════════════════════════════════════

class LeadOut(BaseModel):
    """LeadOut · 完整返回"""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    factory_slug: str
    project_id: Optional[uuid.UUID]
    source: str
    source_campaign: Optional[str]
    source_url: Optional[str]

    contact_id: Optional[uuid.UUID]
    account_id: Optional[uuid.UUID]
    contact_name: Optional[str]
    contact_email: Optional[str]
    contact_phone: Optional[str]
    contact_company: Optional[str]
    contact_country: Optional[str]
    contact_role: Optional[str]

    inquiry_text: str
    inquiry_attachments: Optional[list[dict]]
    inquiry_language: Optional[str]

    # 6 要素
    has_quantity: bool
    has_budget: bool
    has_timeline: bool
    has_specification: bool
    has_decision_role: bool
    has_company_info: bool
    six_factor_score: int
    six_factor_breakdown: Optional[dict]
    classification: Optional[str]
    classification_overridden: bool

    # 状态
    status: str
    assigned_user_id: Optional[uuid.UUID]
    assigned_at: Optional[datetime]
    first_contact_at: Optional[datetime]
    last_activity_at: Optional[datetime]
    qualified_at: Optional[datetime]
    converted_to_deal_id: Optional[uuid.UUID]
    converted_at: Optional[datetime]
    lost_at: Optional[datetime]
    lost_reason: Optional[str]
    lost_competitor: Optional[str]

    # AI
    ai_intent_summary: Optional[str]
    ai_suggested_reply: Optional[str]
    ai_competitors_mentioned: Optional[list[str]]
    ai_translated_zh: Optional[str]
    ai_urgency_score: Optional[float]
    ai_quality_score: Optional[float]

    tags: Optional[list[str]]
    notes: Optional[str]

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
