"""v3 phase 1.3 (2026-05-13): 强制 sha256 + partial unique index 真 dedup

Revision ID: 010_sha256_strict
Revises: 009_crm_core
Create Date: 2026-05-13

═══════════════════════════════════════════════════════════════════════
背景（来自 dam-architecture-audit-2026-05-13.md）：
═══════════════════════════════════════════════════════════════════════

P0 D1 · 今天 watcher V2 重跑产生 ~27 个 duplicate asset：
  - 同 sha256 同 name 同租户 同 project · DAM list 出双份
  - 根因 1：schema 允许 sha256=None · service 落空字符串
  - 根因 2：表上没 (project_id, sha256, alive) 唯一约束 · 并发 race 双插

P0 D2 · processing 状态窗口 dedup 失明：
  - 刚 confirm 的 asset 进 processing · sha256 已写但其他 dedup 拉 list 不见
  - 修复：sha256 在 confirm 时就写（已是当前行为 · 保留）+ 表层 unique 索引

═══════════════════════════════════════════════════════════════════════
本 migration 做三件事：
═══════════════════════════════════════════════════════════════════════

1. 清现存 dup —— 按 (project_id, sha256) 分组 · 保留 created_at 最新一行 ·
   其余 soft-delete（deleted_at=NOW · status=archived）· 不动 R2 对象（留 audit）
2. 加 partial unique index `uq_assets_project_sha_alive`
   ON assets(project_id, sha256) WHERE deleted_at IS NULL AND sha256 <> ''
3. RAISE NOTICE 出 sha256='' 的孤儿行数（人工 review · 不自动删）

注：sha256='' 的行不进 unique 约束（partial WHERE 排除） · 永远不会触发冲突。
   这些行多半是 v2 早期上传 + Cowork watcher 没传 sha256 留下的。
   后续可以单独写个 backfill 任务（HEAD R2 → ETag → 反推 sha 不严谨；
   或 GET 对象重算 · 更准但要钱 · 暂搁置）。

═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "010_sha256_strict"
down_revision: Union[str, None] = "009_crm_core"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── 1) 报告状态 ──────────────────────────────────────────────────
    op.execute(
        """
        DO $$
        DECLARE
            empty_sha_count INT;
            dup_groups INT;
            dup_extras INT;
        BEGIN
            SELECT COUNT(*) INTO empty_sha_count
              FROM assets WHERE (sha256 IS NULL OR sha256 = '') AND deleted_at IS NULL;
            SELECT COUNT(*) INTO dup_groups FROM (
                SELECT project_id, sha256, COUNT(*) AS n
                  FROM assets
                  WHERE deleted_at IS NULL AND sha256 <> ''
                  GROUP BY project_id, sha256
                  HAVING COUNT(*) > 1
            ) g;
            SELECT COALESCE(SUM(n - 1), 0) INTO dup_extras FROM (
                SELECT COUNT(*) AS n
                  FROM assets
                  WHERE deleted_at IS NULL AND sha256 <> ''
                  GROUP BY project_id, sha256
                  HAVING COUNT(*) > 1
            ) g;
            RAISE NOTICE '[010_sha256_strict] empty_sha_alive=% dup_groups=% dup_extras_to_soft_delete=%',
                empty_sha_count, dup_groups, dup_extras;
        END $$;
        """
    )

    # ─── 2) 清现存 dup —— 保留最新 · 其余 soft-delete ────────────────
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY project_id, sha256
                    ORDER BY created_at DESC, id
                ) AS rn
            FROM assets
            WHERE deleted_at IS NULL
              AND sha256 <> ''
        )
        UPDATE assets a
           SET deleted_at = NOW(),
               status = 'archived'
         FROM ranked r
        WHERE a.id = r.id
          AND r.rn > 1;
        """
    )

    # ─── 3) 加 partial unique index ──────────────────────────────────
    # CONCURRENTLY 在 transactional migration 内不能用 ·
    # 锁表时间可接受（assets 12k+ 行 · 几秒内完成）
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_assets_project_sha_alive
        ON assets (project_id, sha256)
        WHERE deleted_at IS NULL AND sha256 <> '';
        """
    )

    # ─── 4) audit 自动记录 dedup 操作（不可篡改 audit_events trigger 保护）─
    # 注：用 system actor · NULL user_id · actor_kind='system'
    # alembic 004 加的 audit_events 表不允许 UPDATE / DELETE · INSERT 永久留痕
    # 列名严格匹配 app/models/audit.py：tenant_id / project_id / actor_user_id
    # / actor_kind / action / target_kind / target_id / status / purpose / ip
    # / user_agent / metadata（实际 DB 列名 metadata · ORM attr extra_metadata）
    #
    # ⚠️ 关于 NOW() 的事务语义（v3 P1.3 phase 5 doc fix · 2026-05-14）：
    # alembic 默认在事务内跑 upgrade()，PG 里 NOW() 在事务中**永远返回 tx-start 时间**·
    # 所以 step 2 写的 deleted_at = NOW() 和此处 NOW() - INTERVAL '1 minute' 都基于同一
    # tx-start。这次能 work 是因为：
    #   step 2 写的 deleted_at = tx_start
    #   step 4 过滤 deleted_at > tx_start - 1min → tx_start > tx_start - 1min = TRUE ✅
    # 但如果**未来有人**把 step 4 移到 autocommit_block 外 / 单独 statement 跑 ·
    # 或者把整个 migration 改成 op.execute_with_autocommit_block · 这个过滤就会
    # 在新事务里取新的 NOW() · 现存"几分钟前归档的行" deleted_at 可能 < 新 NOW() - 1min ·
    # 漏审计。如要重构成 autocommit 模式 · 必须改为按 r.rn > 1 + status='archived' 单独
    # JOIN ranked CTE · 不要依赖时间窗口。
    op.execute(
        """
        INSERT INTO audit_events (
            tenant_id,
            project_id,
            actor_user_id,
            actor_kind,
            action,
            target_kind,
            target_id,
            status,
            purpose,
            ip,
            user_agent,
            metadata
        )
        SELECT
            a.tenant_id,
            a.project_id,
            NULL,
            'system',
            'asset.deleted',
            'asset',
            a.id,
            'success',
            'P0 D1 fix: sha256 strict + partial unique index (alembic 010)',
            NULL,
            NULL,
            jsonb_build_object(
                'migration', '010_sha256_strict',
                'sha256', a.sha256,
                'reason', 'duplicate_with_newer_sibling',
                'actor_label', 'alembic_010_sha256_strict'
            )
        FROM assets a
        WHERE a.status = 'archived'
          AND a.deleted_at > NOW() - INTERVAL '1 minute';
        """
    )


def downgrade() -> None:
    """Downgrade 不还原 soft-delete · 因为可能已被进一步操作过。
    只删 index · 让重复行重新可能 · 旧 client 又能写 sha=''"""
    op.execute("DROP INDEX IF EXISTS uq_assets_project_sha_alive;")
