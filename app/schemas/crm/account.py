"""Pydantic schemas for Account"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class AccountCreate(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=256)
    legal_name: str | None = Field(None, max_length=512)
    country: str | None = Field(None, max_length=64)
    country_code: str | None = Field(None, min_length=2, max_length=2)
    industry: str | None = Field(None, max_length=128)
    sub_industry: str | None = Field(None, max_length=128)
    employee_count: int | None = Field(None, ge=0)
    annual_revenue_usd: int | None = Field(None, ge=0)
    founded_year: int | None = Field(None, ge=1800, le=2100)
    website: str | None = None
    primary_email: EmailStr | None = None
    primary_phone: str | None = Field(None, max_length=64)
    billing_address: dict | None = None
    shipping_address: dict | None = None
    source: str | None = Field(None, max_length=64)
    tags: list[str] | None = None
    notes: str | None = None


class AccountUpdate(BaseModel):
    display_name: str | None = None
    legal_name: str | None = None
    country: str | None = None
    industry: str | None = None
    website: str | None = None
    employee_count: int | None = None
    annual_revenue_usd: int | None = None
    primary_email: EmailStr | None = None
    status: str | None = Field(
        None, pattern="^(active|inactive|archived|spam)$"
    )
    tags: list[str] | None = None
    notes: str | None = None


class AccountMergeIn(BaseModel):
    keep_id: uuid.UUID
    merge_id: uuid.UUID


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    display_name: str
    legal_name: str | None
    country: str | None
    country_code: str | None
    industry: str | None
    sub_industry: str | None
    employee_count: int | None
    annual_revenue_usd: int | None
    founded_year: int | None
    website: str | None
    primary_email: str | None
    primary_phone: str | None
    billing_address: dict | None
    shipping_address: dict | None
    owner_user_id: uuid.UUID | None
    primary_contact_id: uuid.UUID | None
    source: str | None
    status: str
    tags: list[str] | None
    notes: str | None
    ai_company_intel: dict | None
    ai_competitor_score: float | None
    ai_lead_quality_score: float | None
    ai_last_updated_at: datetime | None
    external_ids: dict | None
    created_at: datetime
    updated_at: datetime


class AccountListOut(BaseModel):
    items: list[AccountOut]
    total: int
    limit: int
    offset: int
