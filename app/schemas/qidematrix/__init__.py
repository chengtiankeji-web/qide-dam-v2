"""QideMatrix Pydantic schemas · workspace / subscription / invitation / sso"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

# ─── Plan tier · 跟 alembic 014 CHECK 约束严格对齐 ────────────────────
Plan = Literal["trial", "standard", "enterprise"]
SubscriptionStatus = Literal["active", "past_due", "cancelled", "trial", "expired"]
BillingCycle = Literal["monthly", "yearly"]
MemberRole = Literal["owner", "admin", "member", "viewer"]
InvitationRole = Literal["admin", "member", "viewer"]
PaymentProvider = Literal["wechat", "stripe", "manual"]
Industry = Literal["foreign_trade", "manufacturing", "content", "service", "other"]
WorkflowStatus = Literal["draft", "active", "paused", "archived"]
WorkflowRunStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
TriggerType = Literal["manual", "schedule", "webhook", "event"]


# ─── Plan 配置表 · 单一来源 · 改这里全局生效 ─────────────────────────
PLAN_CONFIG: dict[str, dict] = {
    "trial": {
        "seats": 3,
        "storage_gb": 1,
        "ai_calls_monthly": 100,
        "monthly_price_cny_cents": 0,
        "yearly_price_cny_cents": 0,
        "trial_days": 14,
    },
    "standard": {
        "seats": 15,
        "storage_gb": 100,
        "ai_calls_monthly": 5000,
        "monthly_price_cny_cents": 299_900,  # ¥2,999
        "yearly_price_cny_cents": 2_999_000,  # ¥29,990（送 2 月）
    },
    "enterprise": {
        "seats": 50,
        "storage_gb": 1024,
        "ai_calls_monthly": -1,  # 不限
        "monthly_price_cny_cents": 999_900,  # ¥9,999 起
        "yearly_price_cny_cents": 9_999_000,
    },
}


# ─── Workspace ────────────────────────────────────────────────────────

class WorkspaceCreateIn(BaseModel):
    slug: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
    display_name: str = Field(min_length=1, max_length=200)
    industry: Industry | None = None
    locale: str = "zh-CN"


class WorkspaceUpdateIn(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=200)
    industry: Industry | None = None
    locale: str | None = None
    logo_url: str | None = None
    primary_color: str | None = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")
    custom_domain: str | None = None


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    slug: str
    display_name: str
    owner_user_id: uuid.UUID | None
    plan: Plan
    plan_seats: int
    plan_storage_gb: int
    plan_ai_calls_monthly: int
    trial_ends_at: datetime | None
    industry: str | None
    locale: str
    logo_url: str | None
    primary_color: str | None
    custom_domain: str | None
    created_at: datetime

    class Config:
        from_attributes = True


# ─── Member ───────────────────────────────────────────────────────────

class MemberOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    role: MemberRole
    joined_at: datetime
    last_seen_at: datetime | None = None

    class Config:
        from_attributes = True


class MemberRoleUpdateIn(BaseModel):
    role: MemberRole


# ─── Invitation ───────────────────────────────────────────────────────

class InvitationCreateIn(BaseModel):
    email: EmailStr
    role: InvitationRole = "member"
    expires_in_hours: int = Field(default=72, ge=1, le=720)


class InvitationOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    email: str
    role: InvitationRole
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    token: str | None = None  # 仅创建时返回 · 列表 mask

    class Config:
        from_attributes = True


class InvitationAcceptIn(BaseModel):
    token: str = Field(min_length=32, max_length=64)


# ─── Subscription / Billing ───────────────────────────────────────────

class SubscriptionUpgradeIn(BaseModel):
    target_plan: Plan
    billing_cycle: BillingCycle = "monthly"


class SubscriptionOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    plan: Plan
    status: SubscriptionStatus
    billing_cycle: BillingCycle
    price_cny_cents: int
    started_at: datetime
    current_period_start: datetime
    current_period_end: datetime
    cancel_at_period_end: bool
    cancelled_at: datetime | None
    payment_provider: PaymentProvider | None

    class Config:
        from_attributes = True


class BillingEventOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    subscription_id: uuid.UUID | None
    event_type: str
    amount_cny_cents: int | None
    payment_provider: PaymentProvider | None
    created_at: datetime

    class Config:
        from_attributes = True


# ─── Usage ────────────────────────────────────────────────────────────

class UsageMeterOut(BaseModel):
    workspace_id: uuid.UUID
    period_month: date
    ai_calls_total: int
    ai_tokens_input: int
    ai_tokens_output: int
    ai_cost_cny_cents: int
    storage_bytes: int
    active_users: int
    workflow_runs: int

    class Config:
        from_attributes = True


class UsageSummaryOut(BaseModel):
    workspace_id: uuid.UUID
    plan: Plan
    period_month: date
    ai_calls_used: int
    ai_calls_quota: int  # -1 = unlimited
    ai_calls_pct: float
    storage_used_gb: float
    storage_quota_gb: int
    storage_pct: float
    seats_used: int
    seats_quota: int


# ─── SSO Bridge ───────────────────────────────────────────────────────

class SsoBridgeCreateIn(BaseModel):
    target_workspace_id: uuid.UUID | None = None


class SsoBridgeCreateOut(BaseModel):
    bridge_token: str
    expires_at: datetime
    redirect_url: str


class SsoBridgeRedeemIn(BaseModel):
    bridge_token: str
