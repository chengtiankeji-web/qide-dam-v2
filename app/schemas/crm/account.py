"""Pydantic schemas for Account"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, ConfigDict


class AccountCreate(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=256)
    legal_name: Optional[str] = Field(None, max_length=512)
    country: Optional[str] = Field(None, max_length=64)
    country_code: Optional[str] = Field(None, min_length=2, max_length=2)
    industry: Optional[str] = Field(None, max_length=128)
    sub_industry: Optional[str] = Field(None, max_length=128)
    employee_count: Optional[int] = Field(None, ge=0)
    annual_revenue_usd: Optional[int] = Field(None, ge=0)
    founded_year: Optional[int] = Field(None, ge=1800, le=2100)
    website: Optional[str] = None
    primary_email: Optional[EmailStr] = None
    primary_phone: Optional[str] = Field(None, max_length=64)
    billing_address: Optional[dict] = None
    shipping_address: Optional[dict] = None
    source: Optional[str] = Field(None, max_length=64)
    tags: Optional[list[str]] = None
    notes: Optional[str] = None


class AccountUpdate(BaseModel):
    display_name: Optional[str] = None
    legal_name: Optional[str] = None
    country: Optional[str] = None
    industry: Optional[str] = None
    website: Optional[str] = None
    employee_count: Optional[int] = None
    annual_revenue_usd: Optional[int] = None
    primary_email: Optional[EmailStr] = None
    status: Optional[str] = Field(
        None, pattern="^(active|inactive|archived|spam)$"
    )
    tags: Optional[list[str]] = None
    notes: Optional[str] = None


class AccountMergeIn(BaseModel):
    keep_id: uuid.UUID
    merge_id: uuid.UUID


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    display_name: str
    legal_name: Optional[str]
    country: Optional[str]
    country_code: Optional[str]
    industry: Optional[str]
    sub_industry: Optional[str]
    employee_count: Optional[int]
    annual_revenue_usd: Optional[int]
    founded_year: Optional[int]
    website: Optional[str]
    primary_email: Optional[str]
    primary_phone: Optional[str]
    billing_address: Optional[dict]
    shipping_address: Optional[dict]
    owner_user_id: Optional[uuid.UUID]
    primary_contact_id: Optional[uuid.UUID]
    source: Optional[str]
    status: str
    tags: Optional[list[str]]
    notes: Optional[str]
    ai_company_intel: Optional[dict]
    ai_competitor_score: Optional[float]
    ai_lead_quality_score: Optional[float]
    ai_last_updated_at: Optional[datetime]
    external_ids: Optional[dict]
    created_at: datetime
    updated_at: datetime


class AccountListOut(BaseModel):
    items: list[AccountOut]
    total: int
    limit: int
    offset: int
