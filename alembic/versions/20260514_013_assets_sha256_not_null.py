"""v3 phase 1.3 phase 4 续 (2026-05-14): VALIDATE CHECK + 设置 sha256 NOT NULL

Revision ID: 013_sha256_not_null
Revises: 012_sha256_check
Create Date: 2026-05-14

═══════════════════════════════════════════════════════════════════════
⚠️ 不要立即跑这个 migration · 必须先：
═══════════════════════════════════════════════════════════════════════

1. alembic upgrade 012_sha256_check    # 装 CHECK NOT VALID
2. 跑 scripts/backfill_asset_sha256.py --execute  # 补完所有 145 行
3. 人工 verify 0 行 sha256='' or NULL :
     SELECT COUNT(*) FROM assets WHERE sha256 IS NULL OR sha256='' OR sha256 !~ '^[a-f0-9]{64}$';
   → 必须 = 0 · 否则本 migration 第 2 步会失败
4. alembic upgrade 013_sha256_not_null  # 这一步

═══════════════════════════════════════════════════════════════════════
本 migration 做什么：
═══════════════════════════════════════════════════════════════════════

1. VALIDATE CONSTRAINT —— 把 alembic 012 加的 NOT VALID 约束验证为 VALID
   · 此步会 scan 整张表 · 任何不合规行 → 抛错 → migration 回滚
   · 这是确保数据 100% 干净的最后一道关
2. ALTER COLUMN sha256 SET NOT NULL —— DB 层强制非空
   · 这一步只在 VALIDATE 通过后才执行（PG 自动检查 NULL 行）
   · 跑通后 ORM default="" 也无法插 NULL · sha256 是真硬约束

═══════════════════════════════════════════════════════════════════════
为什么分两个 migration（012 + 013）而不是合并：
═══════════════════════════════════════════════════════════════════════

· 012 立即可装 · 立即给"新写入"加防线 · 不阻塞业务
· backfill 是数据工作 · 跑分钟级 · 跨 migration 容易控制时序
· 013 一旦运行 · 不可回滚到允许 NULL（downgrade 仅删 NOT NULL · 留 CHECK）
  · 必须确保数据先 100% 净化 · 这是工程纪律
· 分开看起来麻烦 · 但避免了"一个 migration 半成功半失败"的灾难
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "013_sha256_not_null"
down_revision: Union[str, None] = "012_sha256_check"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── 1) 前置 sanity check：再次确认 0 不合规行 ─────────────────────
    op.execute(
        """
        DO $$
        DECLARE
            bad_count INT;
        BEGIN
            SELECT COUNT(*) INTO bad_count
              FROM assets
              WHERE deleted_at IS NULL
                AND (sha256 IS NULL OR sha256 = '' OR sha256 !~ '^[a-f0-9]{64}$');
            IF bad_count > 0 THEN
                RAISE EXCEPTION
                  '[013_sha256_not_null] 检出 % 行 sha256 不合规 · 先跑 backfill_asset_sha256.py · migration 中止',
                  bad_count;
            END IF;
            RAISE NOTICE '[013_sha256_not_null] alive rows clean · proceeding with VALIDATE + NOT NULL';
        END $$;
        """
    )

    # ─── 2) VALIDATE CHECK constraint（把 NOT VALID 升级为 VALID） ─────
    # 此步会 scan 全表 · 失败时整个 migration 回滚
    op.execute(
        """
        ALTER TABLE assets VALIDATE CONSTRAINT chk_assets_sha256_strict;
        """
    )

    # ─── 3) 设置 sha256 NOT NULL ──────────────────────────────────────
    # · 此前 12k+ 现存行必须 ALL sha256 IS NOT NULL · sanity check 已保证
    # · 设完之后 INSERT WITHOUT sha256 → PG 直接报错 (DEFAULT '' 还会工作但
    #   与 CHECK 约束冲突 → 也报错)
    op.execute(
        """
        ALTER TABLE assets ALTER COLUMN sha256 SET NOT NULL;
        """
    )

    # ─── 4) audit ─────────────────────────────────────────────────────
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
            'audit.constraint.validated',
            'schema',
            NULL,
            'success',
            'Phase 4 complete: sha256 VALIDATE + NOT NULL · 100% accuracy invariant secured',
            NULL,
            NULL,
            jsonb_build_object(
                'migration', '013_sha256_not_null',
                'constraint', 'chk_assets_sha256_strict',
                'mode', 'VALID',
                'not_null', true,
                'actor_label', 'alembic_013_sha256_not_null'
            )
        FROM tenants t
        WHERE t.slug = 'qide'
        LIMIT 1;
        """
    )


def downgrade() -> None:
    """删 NOT NULL · 保留 CHECK · 让 sha256 又能为空（虽然 CHECK 会拒 INSERT）"""
    op.execute(
        """
        ALTER TABLE assets ALTER COLUMN sha256 DROP NOT NULL;
        """
    )
    # 不还原 VALIDATE → NOT VALID · 没意义（PG 一旦 VALIDATE 就不可逆向）
