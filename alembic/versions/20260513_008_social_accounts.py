"""social_accounts + social_credentials · Social Matrix v2

Revision ID: 008_social_accounts
Revises: 007_intake_jobs
Create Date: 2026-05-13

Social Matrix v2 · 5 个 Developer App OAuth + 100 工厂 token 安全存储
  - 走 Tier 1 官方 API · 不碰 Tier 3 浏览器矩阵（一年内 23-40% 封号）
  - 5 Developer Apps 全部用青玄主体（HK CR 79771658）
  - access_token / refresh_token 必须 Vault 加密（AES-256-GCM · KEK 包 DEK · AAD 绑 platform+factory）

⚠️ 顺序：007 (intake) → 008 (social) → 009 (crm)
   009_crm_core 已先于 008 写好 · social_oauth callback 可写 lead.source_share_link_id
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "008_social_accounts"
down_revision = "007_intake_jobs"


def upgrade() -> None:
    # ════════════════════════════════════════════════════════
    # 1. social_credentials · 凭证安全存储（先建·account 要 FK 进来）
    # ════════════════════════════════════════════════════════
    op.create_table(
        "social_credentials",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("platform", sa.String(32), nullable=False, index=True,
                  comment="linkedin/meta/instagram/tiktok-business/youtube/x"),
        sa.Column("credential_type", sa.String(32), nullable=False,
                  comment="oauth2/oauth1/api-key/long-lived-token"),
        # 加密 payload（与 vault_items 同形 · AAD 绑 platform）
        sa.Column("kek_id", sa.String(32), nullable=False,
                  comment="vault_key_material.id 引用 · 不外键防级联"),
        sa.Column("dek_wrapped", sa.LargeBinary, nullable=False,
                  comment="DEK ciphertext (KEK 加密)"),
        sa.Column("dek_nonce", sa.LargeBinary, nullable=False),
        sa.Column("payload_ciphertext", sa.LargeBinary, nullable=False,
                  comment="JSON {access_token, refresh_token, expires_at, scopes, ...} 的 AES-256-GCM"),
        sa.Column("payload_nonce", sa.LargeBinary, nullable=False),
        sa.Column("payload_tag", sa.LargeBinary, nullable=False),
        # 元数据
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True),
                  comment="token 过期时间·null = 永久"),
        sa.Column("refresh_failed_at", sa.TIMESTAMP(timezone=True),
                  comment="refresh 失败时间·非空时表示 disconnected"),
        sa.Column("scopes", sa.Text,
                  comment="授权的 scope · 逗号分隔"),
        sa.Column("created_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True)),
        sa.CheckConstraint(
            "credential_type IN ('oauth2', 'oauth1', 'api-key', 'long-lived-token')",
            name="ck_social_credentials_type",
        ),
        sa.CheckConstraint(
            "platform IN ('linkedin', 'linkedin-personal', 'meta-page', "
            "'instagram-business', 'tiktok-business', 'youtube', 'x')",
            name="ck_social_credentials_platform",
        ),
    )

    # ════════════════════════════════════════════════════════
    # 2. social_accounts · 工厂 × 平台
    # ════════════════════════════════════════════════════════
    op.create_table(
        "social_accounts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("project_id", UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("factory_slug", sa.String(64), nullable=False, index=True),
        sa.Column("platform", sa.String(32), nullable=False, index=True),
        sa.Column("platform_account_id", sa.String(128), nullable=False,
                  comment="平台返回的 unique id · LinkedIn URN / Page ID / TikTok open_id"),
        sa.Column("display_name", sa.String(256)),
        sa.Column("profile_url", sa.Text),
        sa.Column("avatar_url", sa.Text),
        sa.Column("credential_id", UUID(as_uuid=True),
                  sa.ForeignKey("social_credentials.id", ondelete="SET NULL")),
        # 健康状态
        sa.Column("status", sa.String(32), nullable=False, server_default="active",
                  index=True),
        sa.Column("last_post_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_check_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("warning_count", sa.Integer, server_default="0"),
        sa.Column("metrics", JSONB, server_default="{}",
                  comment="缓存 follower count / engagement / impressions 等"),
        # 元数据 + 时间
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "factory_slug", "platform", "platform_account_id",
            name="uq_social_accounts",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'suspended', 'disconnected', 'pending_oauth')",
            name="ck_social_accounts_status",
        ),
        sa.CheckConstraint(
            "platform IN ('linkedin', 'linkedin-personal', 'meta-page', "
            "'instagram-business', 'tiktok-business', 'youtube', 'x')",
            name="ck_social_accounts_platform",
        ),
    )

    # ════════════════════════════════════════════════════════
    # 3. social_posts · 帖子草稿 / 已发布 / 已撤回
    # ════════════════════════════════════════════════════════
    op.create_table(
        "social_posts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("project_id", UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("account_id", UUID(as_uuid=True),
                  sa.ForeignKey("social_accounts.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("factory_slug", sa.String(64), nullable=False, index=True),
        # 内容
        sa.Column("content_text", sa.Text, nullable=False),
        sa.Column("content_language", sa.String(8), server_default="en"),
        sa.Column("asset_ids", JSONB, server_default="[]",
                  comment="附图 / 视频的 DAM asset_id 列表"),
        sa.Column("link_url", sa.Text,
                  comment="如转发外链文章·LinkedIn 帖子卡片用"),
        # 状态机
        sa.Column("status", sa.String(32), nullable=False, server_default="draft",
                  index=True),
        sa.Column("scheduled_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("platform_post_id", sa.String(256),
                  comment="平台返回的 post ID · LinkedIn share URN / FB post id"),
        sa.Column("platform_post_url", sa.Text),
        # 指标（webhook / 定期拉取回填）
        sa.Column("metrics_likes", sa.Integer, server_default="0"),
        sa.Column("metrics_comments", sa.Integer, server_default="0"),
        sa.Column("metrics_shares", sa.Integer, server_default="0"),
        sa.Column("metrics_impressions", sa.Integer, server_default="0"),
        sa.Column("metrics_clicks", sa.Integer, server_default="0"),
        sa.Column("metrics_last_synced_at", sa.TIMESTAMP(timezone=True)),
        # 错误
        sa.Column("error_message", sa.Text),
        sa.Column("retry_count", sa.Integer, server_default="0"),
        # 审计
        sa.Column("created_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("approved_by_user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'approved', 'scheduled', "
            "'publishing', 'published', 'failed', 'deleted')",
            name="ck_social_posts_status",
        ),
        sa.Index("ix_social_posts_account_status", "account_id", "status"),
        sa.Index(
            "ix_social_posts_scheduled",
            "scheduled_at",
            postgresql_where=sa.text("status = 'scheduled'"),
        ),
    )


def downgrade() -> None:
    op.drop_table("social_posts")
    op.drop_table("social_accounts")
    op.drop_table("social_credentials")
