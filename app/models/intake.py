"""Smart Intake v4 · ORM models · 与 alembic 007_intake_jobs 表对齐

3 张表：
  - IntakeJob · 每次"整理"作业一行
  - IntakeItem · 每文件一行
  - IntakeCluster · SKU 聚类

设计要点：
  - 状态机用 CHECK + 字符串列（与 leads/quotes 一致）
  - JSONB 字段（entity_yml / user_override / category_breakdown）便于 LLM 输出
  - cluster_id 用 SET NULL FK（聚类是辅助·删 cluster 不应级联删 items）
  - 双向 relationship 走 back_populates 防 stale cache
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from sqlalchemy import (
    String, Text, Boolean, Integer, BigInteger, Float, DateTime, Numeric,
    ForeignKey, CheckConstraint, UniqueConstraint, Index, text,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.asset import Asset


# ════════════════════════════════════════════════════════
# IntakeJob · 每次"整理"作业一行
# ════════════════════════════════════════════════════════

class IntakeJob(Base):
    __tablename__ = "intake_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    factory_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_path: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="本地 / mount 路径·必须在 INTAKE_ALLOWED_ROOTS 内",
    )

    # 状态机
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="scanning", index=True,
    )
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,
    )

    # 统计
    total_files: Mapped[int] = mapped_column(Integer, server_default="0")
    classified_count: Mapped[int] = mapped_column(Integer, server_default="0")
    flagged_count: Mapped[int] = mapped_column(Integer, server_default="0")
    duplicate_count: Mapped[int] = mapped_column(Integer, server_default="0")
    clusters_count: Mapped[int] = mapped_column(Integer, server_default="0")
    pushed_count: Mapped[int] = mapped_column(Integer, server_default="0")
    push_error_count: Mapped[int] = mapped_column(Integer, server_default="0")

    # 成本追踪（cost in CNY 估算 + tokens 输入/输出明细）
    llm_cost_cny: Mapped[float] = mapped_column(
        Numeric(10, 4), server_default="0",
    )
    llm_tokens_input: Mapped[int] = mapped_column(Integer, server_default="0")
    llm_tokens_output: Mapped[int] = mapped_column(Integer, server_default="0")

    # 输出
    entity_yml: Mapped[Optional[dict]] = mapped_column(JSONB)
    manifest_storage_key: Mapped[Optional[str]] = mapped_column(Text)
    options: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))

    # 时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
    )
    scan_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    review_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    approved_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    failed_reason: Mapped[Optional[str]] = mapped_column(Text)

    # 关系
    items: Mapped[list["IntakeItem"]] = relationship(
        "IntakeItem", back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    clusters: Mapped[list["IntakeCluster"]] = relationship(
        "IntakeCluster", back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('scanning', 'classifying', 'clustering', 'parsing_docs', "
            "'visual_audit', 'finalizing', 'reviewing', 'approved', 'pushing', "
            "'pushed', 'rejected', 'failed', 'cancelled')",
            name="ck_intake_jobs_status",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<IntakeJob id={self.id} factory={self.factory_slug} "
            f"status={self.status} total={self.total_files}>"
        )


# ════════════════════════════════════════════════════════
# IntakeItem · 每文件一行
# ════════════════════════════════════════════════════════

class IntakeItem(Base):
    __tablename__ = "intake_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intake_jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # 文件元数据
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mime_type: Mapped[Optional[str]] = mapped_column(String(128))
    kind: Mapped[Optional[str]] = mapped_column(String(16))

    # LLM 输出（分类）
    predicted_category: Mapped[Optional[str]] = mapped_column(String(64))
    predicted_sku_slug: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    predicted_subdir: Mapped[Optional[str]] = mapped_column(String(512))
    predicted_target_filename: Mapped[Optional[str]] = mapped_column(String(512))
    predicted_tags: Mapped[Optional[list[str]]] = mapped_column(ARRAY(String(128)))
    confidence: Mapped[float] = mapped_column(Float, server_default="0")
    flagged_reason: Mapped[Optional[str]] = mapped_column(String(256))

    # SKU 聚类
    cluster_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intake_clusters.id", ondelete="SET NULL"),
    )

    # 视觉增强
    visual_verified: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    visual_dominant_colors: Mapped[Optional[dict]] = mapped_column(JSONB)

    # 用户决策
    user_decision: Mapped[Optional[str]] = mapped_column(String(16))
    user_override: Mapped[Optional[dict]] = mapped_column(JSONB)
    user_decision_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # push 结果
    pushed_asset_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="SET NULL"),
        index=True,
    )
    push_error: Mapped[Optional[str]] = mapped_column(Text)
    pushed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"),
    )

    # 关系
    job: Mapped["IntakeJob"] = relationship("IntakeJob", back_populates="items")
    cluster: Mapped[Optional["IntakeCluster"]] = relationship(
        "IntakeCluster", back_populates="items", foreign_keys=[cluster_id],
    )

    __table_args__ = (
        UniqueConstraint("job_id", "sha256", name="uq_intake_items_job_sha"),
        Index("ix_intake_items_job_category", "job_id", "predicted_category"),
        Index(
            "ix_intake_items_job_flagged",
            "job_id", "flagged_reason",
            postgresql_where=text("flagged_reason IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<IntakeItem id={self.id} job={self.job_id} "
            f"cat={self.predicted_category} sku={self.predicted_sku_slug}>"
        )


# ════════════════════════════════════════════════════════
# IntakeCluster · SKU 聚类
# ════════════════════════════════════════════════════════

class IntakeCluster(Base):
    __tablename__ = "intake_clusters"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intake_jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    sku_slug: Mapped[str] = mapped_column(String(128), nullable=False)
    sku_name_cn: Mapped[Optional[str]] = mapped_column(String(256))
    sku_name_en: Mapped[Optional[str]] = mapped_column(String(256))
    subcategory: Mapped[Optional[str]] = mapped_column(String(64))

    item_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    representative_item_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intake_items.id", ondelete="SET NULL"),
    )
    category_breakdown: Mapped[Optional[dict]] = mapped_column(JSONB)

    user_confirmed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    user_renamed_slug: Mapped[Optional[str]] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"),
    )

    # 关系
    job: Mapped["IntakeJob"] = relationship("IntakeJob", back_populates="clusters")
    items: Mapped[list["IntakeItem"]] = relationship(
        "IntakeItem", back_populates="cluster",
        foreign_keys="IntakeItem.cluster_id",
    )

    __table_args__ = (
        UniqueConstraint("job_id", "sku_slug", name="uq_intake_clusters_job_sku"),
    )

    def __repr__(self) -> str:
        return (
            f"<IntakeCluster id={self.id} sku={self.sku_slug} "
            f"count={self.item_count}>"
        )
