"""Pydantic schemas for Contact"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, ConfigDict


class ContactCreate(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=256)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=64)
    title: Optional[str] = Field(None, max_length=256)
    role_category: Optional[str] = Field(
        None, pattern="^(decision_maker|influencer|user|admin|gatekeeper|unknown)$"
    )
    department: Optional[str] = Field(None, max_length=128)
    seniority_level: Optional[str] = Field(None, max_length=64)
    account_id: Optional[uuid.UUID] = None
    linkedin_url: Optional[str] = None
    whatsapp: Optional[str] = Field(None, max_length=64)
    wechat: Optional[str] = Field(None, max_length=128)
    source: Optional[str] = Field(None, max_length=64)
    tags: Optional[list[str]] = None
    notes: Optional[str] = None


class ContactUpdate(BaseModel):
    full_name: Optional[str] = None
    title: Optional[str] = None
    role_category: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    account_id: Optional[uuid.UUID] = None
    linkedin_url: Optional[str] = None
    opt_in_marketing: Optional[bool] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    account_id: Optional[uuid.UUID]
    full_name: str
    first_name: Optional[str]
    last_name: Optional[str]
    title: Optional[str]
    role_category: Optional[str]
    department: Optional[str]
    seniority_level: Optional[str]
    email: Optional[str]
    email_verified_at: Optional[datetime]
    phone: Optional[str]
    mobile: Optional[str]
    whatsapp: Optional[str]
    wechat: Optional[str]
    linkedin_url: Optional[str]
    owner_user_id: Optional[uuid.UUID]
    source: Optional[str]
    status: str
    opt_in_marketing: bool
    unsubscribed_at: Optional[datetime]
    bounced: bool
    tags: Optional[list[str]]
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime


class ContactListOut(BaseModel):
    items: list[ContactOut]
    total: int
    limit: int
    offset: int
