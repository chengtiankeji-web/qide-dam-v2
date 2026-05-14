"""Pydantic schemas for Contact"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ContactCreate(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=256)
    email: EmailStr | None = None
    phone: str | None = Field(None, max_length=64)
    title: str | None = Field(None, max_length=256)
    role_category: str | None = Field(
        None, pattern="^(decision_maker|influencer|user|admin|gatekeeper|unknown)$"
    )
    department: str | None = Field(None, max_length=128)
    seniority_level: str | None = Field(None, max_length=64)
    account_id: uuid.UUID | None = None
    linkedin_url: str | None = None
    whatsapp: str | None = Field(None, max_length=64)
    wechat: str | None = Field(None, max_length=128)
    source: str | None = Field(None, max_length=64)
    tags: list[str] | None = None
    notes: str | None = None


class ContactUpdate(BaseModel):
    full_name: str | None = None
    title: str | None = None
    role_category: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    account_id: uuid.UUID | None = None
    linkedin_url: str | None = None
    opt_in_marketing: bool | None = None
    tags: list[str] | None = None
    notes: str | None = None


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    account_id: uuid.UUID | None
    full_name: str
    first_name: str | None
    last_name: str | None
    title: str | None
    role_category: str | None
    department: str | None
    seniority_level: str | None
    email: str | None
    email_verified_at: datetime | None
    phone: str | None
    mobile: str | None
    whatsapp: str | None
    wechat: str | None
    linkedin_url: str | None
    owner_user_id: uuid.UUID | None
    source: str | None
    status: str
    opt_in_marketing: bool
    unsubscribed_at: datetime | None
    bounced: bool
    tags: list[str] | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class ContactListOut(BaseModel):
    items: list[ContactOut]
    total: int
    limit: int
    offset: int
