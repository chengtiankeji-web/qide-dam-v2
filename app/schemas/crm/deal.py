"""Pydantic schemas for Deal"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict


class DealCreate(BaseModel):
    factory_slug: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=256)
    account_id: Optional[uuid.UUID] = None
    primary_contact_id: Optional[uuid.UUID] = None
    lead_id: Optional[uuid.UUID] = None
    estimated_value_usd: Optional[Decimal] = Field(None, ge=0)
    probability_pct: int = Field(10, ge=0, le=100)
    expected_close_date: Optional[date] = None
    related_sku_slugs: Optional[list[str]] = None
    owner_user_id: Optional[uuid.UUID] = None


class DealStageTransitionIn(BaseModel):
    new_stage: str = Field(
        ..., pattern="^(prospect|qualified|proposal|negotiation|closed_won|closed_lost|on_hold)$"
    )
    won_value_usd: Optional[Decimal] = Field(None, ge=0)
    lost_reason: Optional[str] = Field(None, max_length=128)
    lost_competitor: Optional[str] = Field(None, max_length=256)


class DealOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    factory_slug: str
    name: str
    account_id: Optional[uuid.UUID]
    primary_contact_id: Optional[uuid.UUID]
    lead_id: Optional[uuid.UUID]
    stage: str
    stage_changed_at: Optional[datetime]
    estimated_value_usd: Optional[Decimal]
    probability_pct: Optional[int]
    weighted_value_usd: Optional[Decimal]
    currency: str
    related_sku_slugs: Optional[list[str]]
    related_quote_ids: Optional[list[uuid.UUID]]
    expected_close_date: Optional[date]
    actual_close_date: Optional[date]
    owner_user_id: Optional[uuid.UUID]
    won_at: Optional[datetime]
    won_value_usd: Optional[Decimal]
    lost_at: Optional[datetime]
    lost_reason: Optional[str]
    lost_competitor: Optional[str]
    tags: Optional[list[str]]
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime


class DealListOut(BaseModel):
    items: list[DealOut]
    total: int
    limit: int
    offset: int


class PipelineStageStats(BaseModel):
    stage: str
    count: int
    total_estimated_usd: float
    total_weighted_usd: float


class PipelineForecastOut(BaseModel):
    by_stage: list[PipelineStageStats]
