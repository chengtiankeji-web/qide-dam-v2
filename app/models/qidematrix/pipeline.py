"""QideMatrix v1 · 8 阶段事件总线 + 入驻 + 诊断 + 邮件 models

PipelineEvent · 不可改事件流（PostgreSQL 触发器层保护 · audit-style）
Onboarding · S1 客户入驻申请（来自 CMH /factory-apply）
Diagnostic · S2 AI 出海诊断报告 + PDF 链接
EmailOutbox · 邮件 outbox · Resend/SMTP 发送 · 5 次重试 + 死信
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    pass


# ─── 1. PipelineEvent · 事件总线 ─────────────────────────────────────

class QmPipelineEvent(Base):
    """事件总线 · 不可改 (immutable) · audit-style

    14 个 event_type：
      onboarding.submitted / onboarding.completed
      diagnostic.requested / diagnostic.ready / diagnostic.failed
      dam.workspace_ready
      social.matrix_requested / social.matrix_ready
      content.scheduled / content.published
      lead.qualified / lead.converted
      order.placed / order.delivered

    8 个 stage: S1 ~ S8
    actor_kind: system / user / ai_agent / external
    status state machine: pending → processing → delivered (or failed → parked)
    """
    __tablename__ = "qm_pipeline_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="CASCADE"),
        nullable=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[str] = mapped_column(String(4), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="system")
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    subject_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<QmPipelineEvent {self.event_type} stage={self.stage} status={self.status}>"


# ─── 2. Onboarding · S1 入驻申请 ─────────────────────────────────────

class QmOnboarding(Base):
    """S1 客户入驻申请 · 来源 = CMH /factory-apply

    state machine:
      submitted (S1) → processing (S2 诊断 running)
                    → ready (S2 done · S3 done · 等运营接单)
                    → done (S8 完成 · 客户已激活)
                    → blocked (有 stage 卡 > 7 天)
                    → rejected (运营拒)
    """
    __tablename__ = "qm_onboardings"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )

    # 基本信息
    factory_name: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_name: Mapped[str] = mapped_column(String(100), nullable=False)
    contact_email: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    contact_wechat: Mapped[str | None] = mapped_column(String(100), nullable=True)
    website_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    business_license_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    company_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # v1 升级字段
    product_categories: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    target_markets: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    export_stage: Mapped[str | None] = mapped_column(String(20), nullable=True)
    existing_social_urls: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    monthly_budget: Mapped[str | None] = mapped_column(String(20), nullable=True)
    desired_services: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    top_skus: Mapped[str | None] = mapped_column(Text, nullable=True)
    biggest_pain_point: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 来源 + 状态机
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="cmh_factory_apply")
    source_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    current_stage: Mapped[str] = mapped_column(String(4), nullable=False, default="S1")
    stage_status: Mapped[str] = mapped_column(String(20), nullable=False, default="submitted")
    assigned_operator_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    asset_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(PGUUID(as_uuid=True)), default=list)
    diagnostic_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    extra_metadata: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ─── 3. Diagnostic · S2 AI 诊断报告 ─────────────────────────────────

class QmDiagnostic(Base):
    """S2 AI 出海诊断报告 · LLM + PDF

    5 维度评分 + 30/90/365 天路线图 + 推荐 tier。
    LLM 输出 JSON · reportlab 渲染 PDF · 上传 DAM · 邮件发客户。
    """
    __tablename__ = "qm_diagnostics"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    onboarding_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_onboardings.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )

    # LLM 元信息
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    model_provider: Mapped[str] = mapped_column(String(50), nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 评分
    readiness_score: Mapped[int] = mapped_column(Integer, nullable=False)
    brand_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ops_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compliance_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 输出
    recommended_tier: Mapped[str] = mapped_column(String(20), nullable=False)
    recommended_plan: Mapped[str | None] = mapped_column(String(50), nullable=True)
    industry_benchmark: Mapped[dict] = mapped_column(JSONB, default=dict)
    roadmap_30d: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    roadmap_90d: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    roadmap_365d: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    risks: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    executive_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_report_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)

    # PDF
    pdf_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )
    pdf_signed_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_signed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ─── 4. EmailOutbox · 邮件队列 ─────────────────────────────────────

class QmEmailOutbox(Base):
    """邮件 outbox · status: queued → sending → sent (or failed)

    5 模板：
      welcome / diagnostic_ready / social_ready / first_lead / monthly_report
    """
    __tablename__ = "qm_email_outbox"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_workspaces.id", ondelete="SET NULL"),
        nullable=True,
    )

    template_key: Mapped[str] = mapped_column(String(64), nullable=False)
    locale: Mapped[str] = mapped_column(String(10), nullable=False, default="zh-CN")

    to_email: Mapped[str] = mapped_column(String(200), nullable=False)
    to_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cc_emails: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    bcc_emails: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    from_email: Mapped[str] = mapped_column(
        String(200), nullable=False, default="no-reply@qidelinktech.cn"
    )
    reply_to: Mapped[str | None] = mapped_column(String(200), nullable=True)

    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachments: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    template_vars: Mapped[dict] = mapped_column(JSONB, default=dict)

    onboarding_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_onboardings.id", ondelete="SET NULL"),
        nullable=True,
    )
    diagnostic_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_diagnostics.id", ondelete="SET NULL"),
        nullable=True,
    )
    related_event_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("qm_pipeline_events.id", ondelete="SET NULL"),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    send_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    provider_msg_id: Mapped[str | None] = mapped_column(String(200), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
