"""QideMatrix v1 · S1-S8 业务流 Pydantic schemas"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field

# ─── 枚举 · 跟 alembic 017 CHECK 约束严格对齐 ────────────────────────

Stage = Literal["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"]
StageOrDone = Literal["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "done"]
StageStatus = Literal["submitted", "processing", "blocked", "ready", "done", "rejected"]
HealthStatus = Literal["green", "yellow", "red", "idle"]
EventStatus = Literal["pending", "processing", "delivered", "failed", "parked"]
ActorKind = Literal["system", "user", "ai_agent", "external"]
ExportStage = Literal["awareness", "tried", "converted", "stable"]
MonthlyBudget = Literal["<500", "500-2000", "2000-5000", "5000+"]
DiagnosticStatus = Literal["pending", "running", "ready", "failed"]
RecommendedTier = Literal["starter", "pro", "enterprise"]
EmailStatus = Literal["queued", "sending", "sent", "failed", "cancelled"]
QuoteStatus = Literal["draft", "sent", "accepted", "rejected", "expired"]
OrderStatus = Literal[
    "pending", "accepted", "in_production", "shipped", "delivered",
    "completed", "cancelled", "disputed",
]
FactoryKind = Literal["own", "cmh", "external"]


# ─── 14 个事件类型 · 跟 service / worker 严格对齐 ───────────────────────

EVENT_TYPES: tuple[str, ...] = (
    "onboarding.submitted",
    "onboarding.completed",
    "diagnostic.requested",
    "diagnostic.ready",
    "diagnostic.failed",
    "dam.workspace_ready",
    "social.matrix_requested",
    "social.matrix_ready",
    "content.scheduled",
    "content.published",
    "lead.qualified",
    "lead.converted",
    "order.placed",
    "order.delivered",
)


# ─── Pipeline Event ────────────────────────────────────────────────────

class PipelineEventOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID | None
    tenant_id: uuid.UUID
    event_type: str
    stage: Stage
    actor_kind: ActorKind
    actor_id: uuid.UUID | None
    subject_kind: str | None
    subject_id: uuid.UUID | None
    payload: dict[str, Any]
    status: EventStatus
    attempts: int
    last_error: str | None
    delivered_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class PipelineEventPublishIn(BaseModel):
    """手动 publish 事件（运营 / debug 用）"""
    event_type: str
    stage: Stage
    workspace_id: uuid.UUID | None = None
    subject_kind: str | None = None
    subject_id: uuid.UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# ─── S1 · Onboarding ─────────────────────────────────────────────────

class OnboardingSubmitIn(BaseModel):
    """S1 入驻申请 · 来自 CMH /factory-apply"""
    factory_name: str = Field(min_length=1, max_length=200)
    contact_name: str = Field(min_length=1, max_length=100)
    contact_email: EmailStr
    contact_phone: str | None = Field(None, max_length=50)
    contact_wechat: str | None = Field(None, max_length=100)
    website_url: str | None = Field(None, max_length=500)
    business_license_number: str | None = Field(None, max_length=50)
    company_description: str | None = None

    # v1 升级字段
    product_categories: list[str] = Field(default_factory=list)
    target_markets: list[str] = Field(default_factory=list)
    export_stage: ExportStage | None = None
    existing_social_urls: list[dict] = Field(default_factory=list)
    monthly_budget: MonthlyBudget | None = None
    desired_services: list[str] = Field(default_factory=list)
    top_skus: str | None = None
    biggest_pain_point: str | None = None

    # 来源
    source: str = "cmh_factory_apply"
    source_ref: str | None = None
    asset_ids: list[uuid.UUID] = Field(default_factory=list)

    # 可选：客户公司归到哪个 tenant（platform_admin 用 · 普通用户走 Principal.tenant_id）
    tenant_id: uuid.UUID | None = None


class OnboardingOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID | None
    factory_name: str
    contact_name: str
    contact_email: str
    contact_phone: str | None
    contact_wechat: str | None
    website_url: str | None
    company_description: str | None

    product_categories: list[str] | None
    target_markets: list[str] | None
    export_stage: str | None
    existing_social_urls: list[dict]
    monthly_budget: str | None
    desired_services: list[str] | None
    top_skus: str | None
    biggest_pain_point: str | None

    source: str
    source_ref: str | None
    current_stage: StageOrDone
    stage_status: StageStatus
    assigned_operator_id: uuid.UUID | None
    asset_ids: list[uuid.UUID]
    diagnostic_id: uuid.UUID | None

    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class OnboardingUpdateIn(BaseModel):
    """运营端更新：派单 / 状态推进 / workspace 关联"""
    stage_status: StageStatus | None = None
    current_stage: StageOrDone | None = None
    assigned_operator_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None


class OnboardingAssignIn(BaseModel):
    """运营点 "接单" → 启 S4 工作流"""
    operator_id: uuid.UUID | None = None
    note: str | None = None


# ─── S2 · Diagnostic ─────────────────────────────────────────────────

class DiagnosticOut(BaseModel):
    id: uuid.UUID
    onboarding_id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID | None
    model_name: str
    model_provider: str
    prompt_tokens: int | None
    completion_tokens: int | None
    readiness_score: int
    brand_score: int | None
    product_score: int | None
    channel_score: int | None
    ops_score: int | None
    compliance_score: int | None
    recommended_tier: RecommendedTier
    recommended_plan: str | None
    industry_benchmark: dict
    roadmap_30d: list[dict]
    roadmap_90d: list[dict]
    roadmap_365d: list[dict]
    risks: list[dict]
    executive_summary: str | None
    pdf_asset_id: uuid.UUID | None
    pdf_signed_url: str | None
    pdf_signed_until: datetime | None
    status: DiagnosticStatus
    error_message: str | None
    generated_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class DiagnosticRegenerateIn(BaseModel):
    """手动重生（debug / Sam 看了不满意）"""
    reason: str | None = None
    override_model: str | None = None


# ─── S4 · 运营接单队列 ──────────────────────────────────────────────

class OperatorQueueOut(BaseModel):
    """运营 dashboard / 新入驻队列页用"""
    onboarding_id: uuid.UUID
    factory_name: str
    contact_name: str
    contact_email: str
    product_categories: list[str] | None
    target_markets: list[str] | None
    monthly_budget: str | None
    current_stage: StageOrDone
    stage_status: StageStatus
    blocked_days: int
    recommended_tier: RecommendedTier | None
    readiness_score: int | None
    diagnostic_status: DiagnosticStatus | None
    assigned_operator_id: uuid.UUID | None
    created_at: datetime


# ─── S6 · Quote 报价 ──────────────────────────────────────────────────

class QuoteCreateIn(BaseModel):
    workspace_id: uuid.UUID
    lead_id: uuid.UUID | None = None
    buyer_email: EmailStr | None = None
    buyer_name: str | None = None
    buyer_country: str | None = Field(None, min_length=2, max_length=2)
    buyer_company: str | None = None

    product_name: str = Field(min_length=1, max_length=200)
    product_sku: str | None = None
    quantity: int = Field(ge=1)
    unit_price_usd: Decimal = Field(ge=0)
    incoterms: str = "FOB"
    lead_time_days: int | None = Field(None, ge=0)
    valid_until: date | None = None

    line_items: list[dict] = Field(default_factory=list)
    generation_method: Literal["ai", "manual", "template"] = "ai"


class QuoteOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID
    lead_id: uuid.UUID | None
    buyer_email: str | None
    buyer_name: str | None
    buyer_country: str | None
    buyer_company: str | None
    product_name: str
    product_sku: str | None
    quantity: int
    unit_price_usd: Decimal
    currency: str
    incoterms: str
    lead_time_days: int | None
    valid_until: date | None
    line_items: list[dict]
    total_value_usd: Decimal | None
    pdf_asset_id: uuid.UUID | None
    nnn_contract_asset_id: uuid.UUID | None
    status: QuoteStatus
    sent_at: datetime | None
    accepted_at: datetime | None
    rejected_reason: str | None
    created_at: datetime

    class Config:
        from_attributes = True


# ─── S7 · Order 派单 ──────────────────────────────────────────────────

class OrderCreateIn(BaseModel):
    workspace_id: uuid.UUID
    quote_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None

    buyer_email: EmailStr | None = None
    buyer_name: str | None = None
    buyer_country: str | None = Field(None, min_length=2, max_length=2)
    shipping_address: dict | None = None

    assigned_factory_kind: FactoryKind
    assigned_factory_id: str | None = None
    assigned_factory_name: str | None = None

    product_line_items: list[dict] = Field(default_factory=list)
    total_value_usd: Decimal = Field(ge=0)
    incoterms: str = "FOB"


class OrderUpdateIn(BaseModel):
    status: OrderStatus | None = None
    current_stage: str | None = None
    chosen_logistics: str | None = None
    tracking_number: str | None = None
    shipped_at: datetime | None = None
    delivered_at: datetime | None = None
    payment_method: str | None = None
    payment_received_at: datetime | None = None
    payment_amount_usd: Decimal | None = None
    hs_codes: list[dict] | None = None
    customs_status: str | None = None


class OrderOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID
    quote_id: uuid.UUID | None
    lead_id: uuid.UUID | None
    order_number: str
    buyer_email: str | None
    buyer_name: str | None
    buyer_country: str | None
    shipping_address: dict | None
    assigned_factory_kind: FactoryKind
    assigned_factory_id: str | None
    assigned_factory_name: str | None
    product_line_items: list[dict]
    total_value_usd: Decimal
    incoterms: str
    logistics_recommendation: dict | None
    chosen_logistics: str | None
    tracking_number: str | None
    shipped_at: datetime | None
    delivered_at: datetime | None
    payment_method: str | None
    payment_received_at: datetime | None
    payment_amount_usd: Decimal | None
    hs_codes: list[dict]
    customs_status: str | None
    status: OrderStatus
    current_stage: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ─── S8 · Health Metrics ──────────────────────────────────────────────

class HealthMetricOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    onboarding_id: uuid.UUID | None
    metric_date: date
    stage: Stage
    stage_status: HealthStatus
    blocked_days: int
    traffic_count: int
    lead_count: int
    qualified_lead_count: int
    order_count: int
    revenue_usd: Decimal
    content_published_count: int
    social_posts_count: int
    platform_breakdown: dict
    geo_breakdown: dict
    created_at: datetime

    class Config:
        from_attributes = True


class LinkHealthSnapshotOut(BaseModel):
    """链路健康度快照 · 给 S8 dashboard 用"""
    workspace_id: uuid.UUID
    factory_name: str | None
    snapshot_date: date
    overall_status: HealthStatus
    stage_statuses: dict[str, HealthStatus]
    blocked_stages: list[str]
    last_event_at: datetime | None
    kpi_30d: dict


# ─── Email Outbox ────────────────────────────────────────────────────

class EmailOutboxCreateIn(BaseModel):
    template_key: str = Field(min_length=1, max_length=64)
    locale: str = "zh-CN"
    to_email: EmailStr
    to_name: str | None = None
    cc_emails: list[str] | None = None
    bcc_emails: list[str] | None = None
    template_vars: dict = Field(default_factory=dict)
    attachments: list[dict] = Field(default_factory=list)
    send_after: datetime | None = None
    workspace_id: uuid.UUID | None = None
    onboarding_id: uuid.UUID | None = None
    diagnostic_id: uuid.UUID | None = None


class EmailOutboxOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID | None
    template_key: str
    locale: str
    to_email: str
    to_name: str | None
    from_email: str
    subject: str
    status: EmailStatus
    send_after: datetime
    attempts: int
    last_error: str | None
    sent_at: datetime | None
    provider: str | None
    created_at: datetime

    class Config:
        from_attributes = True
