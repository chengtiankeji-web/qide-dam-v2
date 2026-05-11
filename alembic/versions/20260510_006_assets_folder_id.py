"""add assets.folder_id column for true folder membership

Revision ID: 006_assets_folder_id
Revises: 005_widen_upload_id
Create Date: 2026-05-10

phase 1.2 (2026-05-10) 文件移动功能：

历史 bug：
  AssetUpdate schema 在 2026-05-08 (phase 1) 加了 `folder_id: UUID | None` 字段，
  PATCH /v1/assets/{id} 的 setattr(asset, 'folder_id', value) 看似能改 —
  但 Asset model 没声明 folder_id 列，setattr 只是给 ORM 实例 dynamic 加属性，
  await db.flush() 不会持久化（columns 才会）。所以 phase 1 的"移动"功能从未真正生效。

修复：
  1. Asset model 加 folder_id ForeignKey('folders.id', ondelete='SET NULL') · 可空
  2. 加索引 ix_assets_folder_id 让 list_assets ?folder_id= 高效
  3. ondelete=SET NULL：folder 删了不连带删 asset，asset 自动放回根

历史数据：
  全部 NULL（"项目根目录"）。Sam 想整理就在 admin SPA 用新移动功能。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "006_assets_folder_id"
down_revision: Union[str, None] = "005_widen_upload_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "assets",
        sa.Column(
            "folder_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_assets_folder_id",
        source_table="assets",
        referent_table="folders",
        local_cols=["folder_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_assets_folder_id", "assets", ["folder_id"])


def downgrade() -> None:
    op.drop_index("ix_assets_folder_id", table_name="assets")
    op.drop_constraint("fk_assets_folder_id", "assets", type_="foreignkey")
    op.drop_column("assets", "folder_id")
