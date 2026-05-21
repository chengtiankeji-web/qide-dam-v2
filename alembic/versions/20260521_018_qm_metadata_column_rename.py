"""QideMatrix v1 hotfix · 把 017 写错的 extra_metadata 列名改回 metadata

Revision ID: 018_qm_metadata_rename
Revises: 017_qm_v1_pipeline
Create Date: 2026-05-21

═══════════════════════════════════════════════════════════════════════
背景：
═══════════════════════════════════════════════════════════════════════
alembic 017 创建 qm_onboardings + qm_orders 时 · SQL 里列名写成
`extra_metadata JSONB` · 但 ORM model 是 `mapped_column("metadata", ...)`
（沿用 qm_workspaces 等已有表的约定）· 名字对不上 · ORM 写入时 500：

    asyncpg.exceptions.UndefinedColumnError:
    column "metadata" of relation "qm_onboardings" does not exist

生产环境（5/21 部署当晚）已经用手动 ALTER 修复了：

    ALTER TABLE qm_onboardings RENAME COLUMN extra_metadata TO metadata;
    ALTER TABLE qm_orders RENAME COLUMN extra_metadata TO metadata;

但 alembic 历史还停在 017 · 下次 fresh deploy 会重蹈覆辙。
018 在生产是 no-op（IF EXISTS 守卫）· 在 fresh deploy 跑 017 之后跑 018
自动 RENAME · 让 schema 跟 model 对齐。
═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "018_qm_metadata_rename"
down_revision: Union[str, None] = "017_qm_v1_pipeline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 用 DO 块 + IF EXISTS 守卫 · 生产已 RENAME 的环境 no-op · fresh deploy 跑得动
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'qm_onboardings' AND column_name = 'extra_metadata'
            ) THEN
                ALTER TABLE qm_onboardings RENAME COLUMN extra_metadata TO metadata;
            END IF;
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'qm_orders' AND column_name = 'extra_metadata'
            ) THEN
                ALTER TABLE qm_orders RENAME COLUMN extra_metadata TO metadata;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'qm_onboardings' AND column_name = 'metadata'
            ) THEN
                ALTER TABLE qm_onboardings RENAME COLUMN metadata TO extra_metadata;
            END IF;
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'qm_orders' AND column_name = 'metadata'
            ) THEN
                ALTER TABLE qm_orders RENAME COLUMN metadata TO extra_metadata;
            END IF;
        END $$;
    """)
