"""Pydantic schemas for Deal"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class DealCreate(BaseModel):
    factory_slug: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=256)
    account_id: uuid.UUID | None = None
    primary_contact_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None
    estimated_value_usd: Decimal | None = Field(None, ge=0)
    probability_pct: int = Field(10, ge=0, le=100)
    expected_close_date: date | None = None
    related_sku_slugs: list[str] | None = None
    owner_user_id: uuid.UUID | None = None


class DealStageTransitionIn(BaseModel):
    new_stage: str = Field(
        ..., pattern="^(prospect|qualified|proposal|negotiation|closed_won|closed_lost|on_hold)$"
    )
    won_value_usd: Decimal | None = Field(None, ge=0)
    lost_reason: str | None = Field(None, max_length=128)
    lost_competitor: str | None = Field(None, max_length=256)


class DealOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    factory_slug: str
    name: str
    account_id: uuid.UUID | None
    primary_contact_id: uuid.UUID | None
    lead_id: uuid.UUID | None
    stage: str
    stage_changed_at: datetime | None
    estimated_value_usd: Decimal | None
    probability_pct: int | None
    weighted_value_usd: Decimal | None
    currency: str
    related_sku_slugs: list[str] | None
    related_quote_ids: list[uuid.UUID] | None
    expected_close_date: date | None
    actual_close_date: date | None
    owner_user_id: uuid.UUID | None
    won_at: datetime | None
    won_value_usd: Decimal | None
    lost_at: datetime | None
    lost_reason: str | None
    lost_competitor: str | None
    tags: list[str] | None
    notes: str | None
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
