"""v3 phase 1.3 phase 4 续 (2026-05-14): sha256 强制不变式的 alembic chain 占位

Revision ID: 013_sha256_not_null
Revises: 012_sha256_check
Create Date: 2026-05-14

═══════════════════════════════════════════════════════════════════════
设计修正（2026-05-14 二修）：
═══════════════════════════════════════════════════════════════════════

第一版试图在本 migration 里做 VALIDATE + NOT NULL，但有个**部署级 bug**：
  - docker-compose.prod.yml 让 api 容器启动时自动跑 `alembic upgrade head`
  - 数据没干净时 RAISE EXCEPTION 会让 api 容器 entrypoint 失败 → 整个 prod 部署卡死

修正：本 migration 只 RAISE NOTICE 提示状态 · 不真做 ALTER TABLE。
     真正的 VALIDATE + NOT NULL 拆到独立脚本 scripts/finalize_sha256_strict.py，
     由人工在 backfill 完成后**显式调用**。

这样：
  · 任何时候 `alembic upgrade head` 都安全（不会阻塞部署）
  · 数据没干净时不破坏 invariant（CHECK NOT VALID 仍然在保护新写入）
  · 数据干净后 · 跑一次 finalize 脚本 · 把 NOT NULL 落地
  · 完成的事实记录在 audit_events（不可篡改）

═══════════════════════════════════════════════════════════════════════
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
    # 仅报告状态 · 不真做 ALTER TABLE。
    # 真正的 VALIDATE + NOT NULL 在 scripts/finalize_sha256_strict.py 里手动跑。
    op.execute(
        """
        DO $$
        DECLARE
            bad_count INT;
            already_not_null BOOLEAN;
        BEGIN
            SELECT COUNT(*) INTO bad_count
              FROM assets
              WHERE deleted_at IS NULL
                AND (sha256 IS NULL OR sha256 = '' OR sha256 !~ '^[a-f0-9]{64}$');

            SELECT (is_nullable = 'NO') INTO already_not_null
              FROM information_schema.columns
              WHERE table_name = 'assets' AND column_name = 'sha256';

            IF already_not_null THEN
                RAISE NOTICE '[013_sha256_not_null] sha256 already NOT NULL · invariant secured';
            ELSIF bad_count > 0 THEN
                RAISE NOTICE
                  '[013_sha256_not_null] PENDING: % bad rows · CHECK NOT VALID still protecting new writes · run scripts/backfill_asset_sha256.py then scripts/finalize_sha256_strict.py',
                  bad_count;
            ELSE
                RAISE NOTICE
                  '[013_sha256_not_null] 0 bad rows · ready for finalize · run: python scripts/finalize_sha256_strict.py';
            END IF;
        END $$;
        """
    )

    # audit 留痕：本 migration 已"应用"（但 invariant 是否真生效取决于 finalize 是否跑过）
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
            'audit.migration.applied',
            'schema',
            NULL,
            'success',
            'Phase 4 marker · 013_sha256_not_null applied (alembic chain advance) · real ALTER deferred to scripts/finalize_sha256_strict.py',
            NULL,
            NULL,
            jsonb_build_object(
                'migration', '013_sha256_not_null',
                'mode', 'marker_only',
                'finalize_script', 'scripts/finalize_sha256_strict.py',
                'actor_label', 'alembic_013_sha256_not_null'
            )
        FROM tenants t
        WHERE t.slug = 'qide'
        LIMIT 1;
        """
    )


def downgrade() -> None:
    """本 migration 仅是 marker · downgrade 无操作"""
    pass
