"""intake_jobs + intake_items + intake_clusters · Smart Intake v4

Revision ID: 007_intake_jobs
Revises: 006_assets_folder_id
Create Date: 2026-05-13

Smart Intake v4 · 把"工厂 raw folder → 结构化 DAM"自动化
  - 整理 23K files / 5GB 工厂资料·30 分钟而非 30+ 工时
  - LLM 自动分类（filename + 视觉 + docx 解析）
  - Sam admin SPA review queue 1-click approve 后推 DAM

⚠️ 与 009_crm_core 顺序：009 depends on 008（social）+ 这个 007 互不依赖
   实际部署顺序：006（已部署）→ 007（本）→ 008（小龙 social）→ 009（Claude crm）
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from alembic import op

revision = "007_intake_jobs"
down_revision = "006_assets_folder_id"


def upgrade() -> None:
    # ════════════════════════════════════════════════════════
    # 1. intake_jobs · 每次"整理"作业一行
    # ════════════════════════════════════════════════════════
    op.create_table(
        "intake_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("project_id", UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("factory_slug", sa.String(64), nullable=False, index=True),
        sa.Column("source_path", sa.Text, nullable=False,
                  comment="本地 / mount 路径·必须在 INTAKE_ALLOWED_ROOTS 内"),
        # 状态机
        sa.Column("status", sa.String(32), nullable=False, server_default="scanning",
                  index=True),
        sa.Column("created_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), index=True),
        # 统计
        sa.Column("total_files", sa.Integer, server_default="0"),
        sa.Column("classified_count", sa.Integer, server_default="0"),
        sa.Column("flagged_count", sa.Integer, server_default="0",
                  comment="需要人工 review 的项"),
        sa.Column("duplicate_count", sa.Integer, server_default="0"),
        sa.Column("clusters_count", sa.Integer, server_default="0"),
        sa.Column("pushed_count", sa.Integer, server_default="0"),
        sa.Column("push_error_count", sa.Integer, server_default="0"),
        # 成本追踪
        sa.Column("llm_cost_cny", sa.Numeric(10, 4), server_default="0",
                  comment="本任务累计 token 成本估算（¥）"),
        sa.Column("llm_tokens_input", sa.Integer, server_default="0"),
        sa.Column("llm_tokens_output", sa.Integer, server_default="0"),
        # 输出
        sa.Column("entity_yml", JSONB,
                  comment="LLM 抽取的 entity 字段·用户可改"),
        sa.Column("manifest_storage_key", sa.Text,
                  comment="生成的 manifest.tsv 在 R2 的 key·历史归档"),
        sa.Column("options", JSONB, server_default="{}",
                  comment="job 选项·{max_files, skip_visual, locale, etc.}"),
        # 时间
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("scan_completed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("review_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("approved_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("failed_reason", sa.Text),
        sa.CheckConstraint(
            "status IN ('scanning', 'classifying', 'clustering', 'parsing_docs', "
            "'visual_audit', 'finalizing', 'reviewing', 'approved', 'pushing', "
            "'pushed', 'rejected', 'failed', 'cancelled')",
            name="ck_intake_jobs_status",
        ),
    )

    # ════════════════════════════════════════════════════════
    # 2. intake_items · 每文件一行
    # ════════════════════════════════════════════════════════
    op.create_table(
        "intake_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", UUID(as_uuid=True),
                  sa.ForeignKey("intake_jobs.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        # 文件元数据
        sa.Column("source_path", sa.Text, nullable=False,
                  comment="zip 内文件用 'path/to.zip:inner.jpg' 格式"),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False, index=True),
        sa.Column("mime_type", sa.String(128)),
        sa.Column("kind", sa.String(16),
                  comment="image/video/document/audio/other"),
        # LLM 输出（分类）
        sa.Column("predicted_category", sa.String(64),
                  comment="license/master/lifestyle/detail/packaging/spec/video/factory/etc."),
        sa.Column("predicted_sku_slug", sa.String(128), index=True),
        sa.Column("predicted_subdir", sa.String(512),
                  comment="完整路径·如 /factories/aozi-cosmetics/sku/yushikou-handcream/master/"),
        sa.Column("predicted_target_filename", sa.String(512),
                  comment="规范化命名·如 yushikou-handcream--master--01.jpg"),
        sa.Column("predicted_tags", ARRAY(sa.String(128))),
        sa.Column("confidence", sa.Float, server_default="0",
                  comment="0-1 · LLM 自评信心"),
        sa.Column("flagged_reason", sa.String(256),
                  comment="low_confidence / duplicate_sha / unknown_format / cross_sku"),
        # SKU 聚类
        sa.Column("cluster_id", UUID(as_uuid=True)),  # FK 后建（cluster table 还没建）
        # 视觉增强（visual_audit task 跑后填）
        sa.Column("visual_verified", sa.Boolean, server_default=sa.text("false")),
        sa.Column("visual_dominant_colors", JSONB,
                  comment="Qwen-VL 抽的 5 主色"),
        # 用户决策
        sa.Column("user_decision", sa.String(16),
                  comment="approve / reject / edit / null"),
        sa.Column("user_override", JSONB,
                  comment="用户改的 subdir/filename/tags"),
        sa.Column("user_decision_at", sa.TIMESTAMP(timezone=True)),
        # push 结果
        sa.Column("pushed_asset_id", UUID(as_uuid=True),
                  sa.ForeignKey("assets.id", ondelete="SET NULL"), index=True),
        sa.Column("push_error", sa.Text),
        sa.Column("pushed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("job_id", "sha256", name="uq_intake_items_job_sha"),
    )
    op.create_index("ix_intake_items_job_category",
                    "intake_items", ["job_id", "predicted_category"])
    op.create_index("ix_intake_items_job_flagged",
                    "intake_items", ["job_id", "flagged_reason"],
                    postgresql_where=sa.text("flagged_reason IS NOT NULL"))

    # ════════════════════════════════════════════════════════
    # 3. intake_clusters · SKU 聚类
    # ════════════════════════════════════════════════════════
    op.create_table(
        "intake_clusters",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_id", UUID(as_uuid=True),
                  sa.ForeignKey("intake_jobs.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("sku_slug", sa.String(128), nullable=False),
        sa.Column("sku_name_cn", sa.String(256)),
        sa.Column("sku_name_en", sa.String(256)),
        sa.Column("subcategory", sa.String(64),
                  comment="如 sofa/bed/mattress for gostoo"),
        sa.Column("item_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("representative_item_id", UUID(as_uuid=True),
                  sa.ForeignKey("intake_items.id", ondelete="SET NULL")),
        sa.Column("category_breakdown", JSONB,
                  comment="{master: 5, detail: 12, video: 1, ...}"),
        sa.Column("user_confirmed", sa.Boolean, server_default=sa.text("false")),
        sa.Column("user_renamed_slug", sa.String(128),
                  comment="如用户改 sku slug · 比如算法说 'sofa-001' BD 说 'modern-leather-l01'"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("job_id", "sku_slug", name="uq_intake_clusters_job_sku"),
    )

    # intake_items.cluster_id FK
    op.create_foreign_key(
        "fk_intake_items_cluster",
        "intake_items", "intake_clusters",
        ["cluster_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_intake_items_cluster", "intake_items", type_="foreignkey")
    op.drop_table("intake_clusters")
    op.drop_table("intake_items")
    op.drop_table("intake_jobs")
