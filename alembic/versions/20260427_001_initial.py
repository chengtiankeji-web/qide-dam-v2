"""initial schema — tenants, projects, users, api_keys, assets, asset_versions

Revision ID: 001_initial
Revises:
Create Date: 2026-04-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ----- extensions -----
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')   # for gen_random_uuid()
    op.execute('CREATE EXTENSION IF NOT EXISTS "pg_trgm"')    # full-text search
    op.execute('CREATE EXTENSION IF NOT EXISTS "vector"')     # pgvector

    # ----- tenants -----
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("legal_entity_type", sa.String(32), nullable=True),
        sa.Column("credit_code", sa.String(32), nullable=True),
        sa.Column("storage_prefix", sa.String(64), nullable=False, unique=True),
        sa.Column("quota_storage_bytes", sa.BigInteger, nullable=False,
                  server_default=sa.text(str(10 * 1024**4))),
        sa.Column("quota_assets", sa.BigInteger, nullable=False,
                  server_default=sa.text("1000000")),
        sa.Column("quota_monthly_uploads_bytes", sa.BigInteger, nullable=False,
                  server_default=sa.text(str(1024**4))),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("settings", sa.JSON, nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_tenants_credit_code", "tenants", ["credit_code"])
    op.create_index("ix_tenants_deleted_at", "tenants", ["deleted_at"])

    # ----- projects -----
    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(1024), nullable=True),
        sa.Column("storage_prefix", sa.String(64), nullable=False),
        sa.Column("default_acl", sa.String(16), nullable=False, server_default="project"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("settings", sa.JSON, nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_projects_tenant_slug"),
    )
    op.create_index("ix_projects_tenant_id", "projects", ["tenant_id"])
    op.create_index("ix_projects_slug", "projects", ["slug"])
    op.create_check_constraint(
        "ck_projects_default_acl_valid", "projects",
        "default_acl IN ('private','project','tenant','public')",
    )

    # ----- users -----
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("password_hash", sa.String(128), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="member"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("is_platform_admin", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("project_access", sa.JSON, nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])
    op.create_index("ix_users_email", "users", ["email"])
    op.create_check_constraint(
        "ck_users_role_valid", "users",
        "role IN ('platform_admin','tenant_admin','member','viewer')",
    )

    # ----- api_keys -----
    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("prefix", sa.String(64), nullable=False),
        sa.Column("key_hash", sa.String(128), nullable=False, unique=True),
        sa.Column("scopes", sa.JSON, nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.create_index("ix_api_keys_project_id", "api_keys", ["project_id"])
    op.create_index("ix_api_keys_prefix", "api_keys", ["prefix"])
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"])

    # ----- assets -----
    op.create_table(
        "assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="other"),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("extension", sa.String(16), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False, unique=True),
        sa.Column("storage_bucket", sa.String(64), nullable=False),
        sa.Column("public_url", sa.String(1024), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="ready"),
        sa.Column("source", sa.String(16), nullable=False, server_default="upload"),
        sa.Column("acl", sa.String(16), nullable=False, server_default="project"),
        sa.Column("width", sa.Integer, nullable=True),
        sa.Column("height", sa.Integer, nullable=True),
        sa.Column("duration_seconds", sa.Float, nullable=True),
        sa.Column("page_count", sa.Integer, nullable=True),
        sa.Column("thumbnails", sa.JSON, nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("technical_metadata", sa.JSON, nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("auto_tags", postgresql.ARRAY(sa.String(64)), nullable=False,
                  server_default=sa.text("'{}'::text[]")),
        sa.Column("manual_tags", postgresql.ARRAY(sa.String(64)), nullable=False,
                  server_default=sa.text("'{}'::text[]")),
        sa.Column("ai_summary", sa.Text, nullable=True),
        sa.Column("ai_alt_text", sa.Text, nullable=True),
        sa.Column("ai_visual_description", sa.Text, nullable=True),
        sa.Column("ai_model", sa.String(64), nullable=True),
        sa.Column("ai_processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("is_starred", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("custom_fields", sa.JSON, nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_assets_tenant_project", "assets", ["tenant_id", "project_id"])
    op.create_index("ix_assets_kind_status", "assets", ["kind", "status"])
    op.create_index("ix_assets_sha256", "assets", ["sha256"])
    op.create_index("ix_assets_deleted_at", "assets", ["deleted_at"])
    op.create_check_constraint(
        "ck_assets_acl_valid", "assets",
        "acl IN ('private','project','tenant','public')",
    )
    op.create_check_constraint(
        "ck_assets_kind_valid", "assets",
        "kind IN ('image','video','audio','document','archive','model3d','other')",
    )
    op.create_check_constraint(
        "ck_assets_status_valid", "assets",
        "status IN ('uploading','processing','ready','failed','archived')",
    )
    op.create_check_constraint(
        "ck_assets_source_valid", "assets",
        "source IN ('upload','migration','mcp','webhook','system')",
    )

    # pgvector embedding column — added via raw SQL since SQLA core lacks Vector type
    op.execute('ALTER TABLE assets ADD COLUMN embedding vector(768)')
    # IVF index for cosine sim — kicks in once we have ~1k+ rows; harmless before then
    op.execute(
        'CREATE INDEX ix_assets_embedding_cos ON assets '
        'USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)'
    )
    # GIN on tag arrays for fast tag filter
    op.execute('CREATE INDEX ix_assets_auto_tags_gin ON assets USING gin (auto_tags)')
    op.execute('CREATE INDEX ix_assets_manual_tags_gin ON assets USING gin (manual_tags)')
    # trigram on name for fuzzy search
    op.execute(
        "CREATE INDEX ix_assets_name_trgm ON assets USING gin (name gin_trgm_ops)"
    )

    # ----- asset_versions -----
    op.create_table(
        "asset_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_no", sa.Integer, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("uploaded_by_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("note", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_asset_versions_asset_version", "asset_versions",
        ["asset_id", "version_no"], unique=True,
    )

    # ----- seed: 5 主体 tenants + 7 default projects -----
    # The init_db.py script creates the platform admin user separately.
    op.execute(
        """
        INSERT INTO tenants (slug, name, display_name, legal_entity_type, credit_code, storage_prefix)
        VALUES
          ('qide',      '佛山祁德商链科技有限公司', '祁德 (中台)',     'limited',     '91440606MAE4BYC210', 'qide'),
          ('qingxuan',  '青玄国际贸易',          '青玄 (HK)',       'limited',     'HK-CR-79771658',     'qingxuan'),
          ('zerun',     '泽润良品',              '泽润 (深圳)',     'limited',     '91440300MAK4ULH9XN', 'zerun'),
          ('hemei',     '和美共创乡村运营促进中心', '和美共创 (顺德 · 民非)', 'non-profit', NULL,                'hemei'),
          ('chengtian', '广州橙天电子商务有限公司', '橙天 (Sam 个人)',  'limited',     '91440101MA5AYL9PXU', 'chengtian')
        ON CONFLICT (slug) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO projects (tenant_id, slug, name, storage_prefix)
        SELECT t.id, p.slug, p.name, p.slug
        FROM tenants t,
             (VALUES
               ('qide',      'core',           '祁德核心'),
               ('qide',      'dam',            '数字资产管理后台'),
               ('qide',      'website',        '祁德官网'),
               ('qide',      'aivisible',      'AiVisible'),
               ('qingxuan',  'kiln-ink',       'Kiln & Ink'),
               ('qingxuan',  'qingxuan-intel', '青玄情报中心'),
               ('zerun',     'cmh',            'ChinaMakersHub'),
               ('hemei',     'xiangyue-shunde','乡约顺德'),
               ('chengtian', 'personal',       '橙天个人档案')
             ) AS p(tenant_slug, slug, name)
        WHERE t.slug = p.tenant_slug
        ON CONFLICT (tenant_id, slug) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("asset_versions")
    op.execute("DROP INDEX IF EXISTS ix_assets_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_assets_manual_tags_gin")
    op.execute("DROP INDEX IF EXISTS ix_assets_auto_tags_gin")
    op.execute("DROP INDEX IF EXISTS ix_assets_embedding_cos")
    op.drop_table("assets")
    op.drop_table("api_keys")
    op.drop_table("users")
    op.drop_table("projects")
    op.drop_table("tenants")
    op.execute('DROP EXTENSION IF EXISTS "vector"')
    op.execute('DROP EXTENSION IF EXISTS "pg_trgm"')
    op.execute('DROP EXTENSION IF EXISTS "pgcrypto"')
