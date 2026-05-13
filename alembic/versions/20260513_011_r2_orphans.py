"""v3 phase 1.3 (2026-05-13): r2_orphans table for unreclaimed R2 objects

Revision ID: 011_r2_orphans
Revises: 010_sha256_strict
Create Date: 2026-05-13

═══════════════════════════════════════════════════════════════════════
背景：handover/dam-architecture-audit-2026-05-13.md · P1 D7

hard_delete_asset 的 R2 删失败时，asset DB 行依然被 delete · R2 对象成永久孤儿 ·
不被任何表引用 · 不被任何 GC 看到 · 计费 forever。

修复：
  1. r2_orphans 表记录每次 R2 删失败的 storage_key + bucket + 失败原因 + 重试计数
  2. tasks_cleanup.retry_r2_orphans 每天指数 backoff 重试 · 成功就 delete row
  3. admin SPA 可以列 r2_orphans 表给 Sam 看 · 也可手动 force_retry
═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "011_r2_orphans"
down_revision: Union[str, None] = "010_sha256_strict"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "r2_orphans",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),  # nullable: 可能来自 system task
        sa.Column("project_id", UUID(as_uuid=True), nullable=True),
        sa.Column("storage_key", sa.String(512), nullable=False, unique=True),
        sa.Column("storage_bucket", sa.String(64), nullable=False),
        sa.Column("origin_asset_id", UUID(as_uuid=True), nullable=True,
                  comment="哪个 asset 触发的 · DB 行已 delete · 仅供溯源"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True,
                  comment="成功删除 R2 后填 · 也可标记 manual_resolved"),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_r2_orphans_pending", "r2_orphans", ["next_retry_at"],
                    postgresql_where=sa.text("resolved_at IS NULL"))
    op.create_index("ix_r2_orphans_tenant", "r2_orphans", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_r2_orphans_tenant", table_name="r2_orphans")
    op.drop_index("ix_r2_orphans_pending", table_name="r2_orphans")
    op.drop_table("r2_orphans")
