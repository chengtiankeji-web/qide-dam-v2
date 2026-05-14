"""v3 phase 1.3 phase 4 (2026-05-14): DB 层强制 sha256 = 64-hex 格式（CHECK NOT VALID）

Revision ID: 012_sha256_check
Revises: 011_r2_orphans
Create Date: 2026-05-14

═══════════════════════════════════════════════════════════════════════
为什么 NOT VALID：
═══════════════════════════════════════════════════════════════════════

alembic 010 加了 partial unique index (project_id, sha256) WHERE sha256 <> '' ·
但 sha256 = '' 或 NULL 的行根本不会进 unique 约束 · 是后门。

本 migration：
  · 加 CHECK 约束要求 sha256 必须 match `^[a-f0-9]{64}$`
  · NOT VALID 模式：约束**仅作用于新写入** · 不 backfill 验证现存 145 行（5/14 audit）
  · 等 scripts/backfill_asset_sha256.py 跑完所有 145 行的 sha 后 ·
    手动 `ALTER TABLE assets VALIDATE CONSTRAINT chk_assets_sha256_strict;`
    把约束从 NOT VALID → VALID（这一步 alembic 013 来做 + 加 NOT NULL）

NOT VALID 的语义保证：
  · INSERT / UPDATE 新值时 PostgreSQL 立即检查 · 不合规直接 reject
  · 现存行的"历史不合规"不阻塞约束创建（重要 · 否则 backfill 没跑就装不上）
  · 约束在 pg_constraint 表里 `convalidated=false` 标记 · 一目了然

═══════════════════════════════════════════════════════════════════════
关于 Asset 模型字段：
═══════════════════════════════════════════════════════════════════════
app/models/asset.py sha256: Mapped[str] = mapped_column(String(64), default="")
  · ORM 层 default 空字符串 · 但 PresignedUploadIn schema (v3 P1.3) pattern
    `^[a-f0-9]{64}$` 已要求新写入非空 · ORM default 仅 fallback safety net。
  · 本 CHECK 让 fallback 也被 DB 层拒掉 · 不再有"sha 漏写"路径。

═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "012_sha256_check"
down_revision: Union[str, None] = "011_r2_orphans"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── 1) 报告状态：现存空 sha 行有多少（人工 review） ───────────────
    op.execute(
        """
        DO $$
        DECLARE
            empty_count INT;
            invalid_format_count INT;
        BEGIN
            SELECT COUNT(*) INTO empty_count
              FROM assets
              WHERE deleted_at IS NULL
                AND (sha256 IS NULL OR sha256 = '');
            SELECT COUNT(*) INTO invalid_format_count
              FROM assets
              WHERE deleted_at IS NULL
                AND sha256 IS NOT NULL
                AND sha256 != ''
                AND sha256 !~ '^[a-f0-9]{64}$';
            RAISE NOTICE '[012_sha256_check] alive_empty_sha=% alive_invalid_format=%',
                empty_count, invalid_format_count;
            IF invalid_format_count > 0 THEN
                RAISE WARNING 'There are % rows with malformed sha256 · backfill needed before VALIDATE',
                    invalid_format_count;
            END IF;
        END $$;
        """
    )

    # ─── 2) 加 CHECK 约束（NOT VALID · 不阻塞存量） ────────────────────
    # 注意约束名：chk_assets_sha256_strict
    # 注意约束逻辑：sha256 必须是 64 位 lowercase hex
    # 注意 NOT VALID：PG 仅约束新行 · 现存行如不合规留待 backfill
    op.execute(
        """
        ALTER TABLE assets
          ADD CONSTRAINT chk_assets_sha256_strict
          CHECK (sha256 ~ '^[a-f0-9]{64}$')
          NOT VALID;
        """
    )

    # ─── 3) audit 留痕（system actor · 不可篡改流） ────────────────────
    op.execute(
        """
        INSERT INTO audit_events (
            tenant_id, project_id, actor_user_id, actor_kind,
            action, target_kind, target_id, status, purpose,
            ip, user_agent, metadata
        )
        SELECT
            t.id,
            NULL,
            NULL,
            'system',
            'audit.constraint.added',
            'schema',
            NULL,
            'success',
            'Phase 4: chk_assets_sha256_strict NOT VALID',
            NULL,
            NULL,
            jsonb_build_object(
                'migration', '012_sha256_check',
                'constraint', 'chk_assets_sha256_strict',
                'mode', 'NOT_VALID',
                'next_step', 'after Phase 3 backfill complete · alembic 013 will VALIDATE + add NOT NULL',
                'actor_label', 'alembic_012_sha256_check'
            )
        FROM tenants t
        WHERE t.slug = 'qide'  -- platform 默认租户 · 单条 audit 留痕足够
        LIMIT 1;
        """
    )


def downgrade() -> None:
    """删 CHECK 约束 · 不影响数据"""
    op.execute(
        """
        ALTER TABLE assets DROP CONSTRAINT IF EXISTS chk_assets_sha256_strict;
        """
    )
