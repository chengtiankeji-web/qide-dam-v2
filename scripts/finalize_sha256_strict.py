#!/usr/bin/env python3
"""scripts/finalize_sha256_strict.py — Phase 4 最终封印

═══════════════════════════════════════════════════════════════════════
设计：
═══════════════════════════════════════════════════════════════════════

为什么是独立脚本不是 alembic migration：
  · docker-compose.prod.yml 让 api 容器 entrypoint 自动跑 `alembic upgrade head`
  · 如果 ALTER 在 alembic migration 里 · 数据没干净时会让 api 容器启动失败
  · 把 ALTER 移到独立脚本 · 让 alembic 永远不阻塞部署
  · finalize 由人工**显式**跑 · 跑前 sanity check 通过才真改表 · 失败安全

═══════════════════════════════════════════════════════════════════════
用法：
═══════════════════════════════════════════════════════════════════════

1. 先跑 scripts/backfill_asset_sha256.py --execute --continue（补完所有空 sha）
2. 再跑 这个脚本：
     sudo docker compose --env-file .env.production exec -T api \\
       python3 scripts/finalize_sha256_strict.py --execute

   --dry-run 模式：只检查不动表
   --execute 模式：真做 VALIDATE + NOT NULL

3. 跑完后 sha256 column 是 DB 层 NOT NULL · 任何空写入都被 PG 拒
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import AsyncSessionLocal  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    if args.dry_run == args.execute:
        print("ERROR: 必须二选一 --dry-run 或 --execute", file=sys.stderr)
        return 1

    async with AsyncSessionLocal() as db:
        # ── 1) 前置 sanity check ───────────────────────────────────────
        bad_row = (
            await db.execute(
                text(
                    """
                    SELECT COUNT(*) FROM assets
                      WHERE deleted_at IS NULL
                        AND (sha256 IS NULL OR sha256 = '' OR sha256 !~ '^[a-f0-9]{64}$');
                    """
                )
            )
        ).scalar_one()

        nullable_row = (
            await db.execute(
                text(
                    """
                    SELECT is_nullable FROM information_schema.columns
                      WHERE table_name = 'assets' AND column_name = 'sha256';
                    """
                )
            )
        ).scalar_one()
        already_not_null = (nullable_row == "NO")

        constraint_row = (
            await db.execute(
                text(
                    """
                    SELECT convalidated FROM pg_constraint
                      WHERE conname = 'chk_assets_sha256_strict';
                    """
                )
            )
        ).scalar_one_or_none()
        constraint_state = (
            "missing" if constraint_row is None
            else "VALID" if constraint_row
            else "NOT VALID"
        )

        print("=== sha256 强制状态检查 ===")
        print(f"  不合规 alive 行数: {bad_row}")
        print(f"  sha256 column NULL 状态: {'NOT NULL ✓' if already_not_null else 'NULLable (待 finalize)'}")
        print(f"  chk_assets_sha256_strict 约束: {constraint_state}")
        print()

        if bad_row > 0:
            print(f"✗ 还有 {bad_row} 行 sha256 不合规 · 先跑 backfill_asset_sha256.py")
            print(f"  sudo docker compose --env-file .env.production exec -T api \\")
            print(f"    python3 scripts/backfill_asset_sha256.py --execute --continue")
            return 2

        if already_not_null and constraint_state == "VALID":
            print("✓ 已经完成 finalize · 不需要重跑")
            return 0

        if args.dry_run:
            print("✓ 数据干净 · 可以 finalize")
            print()
            print("将要执行:")
            if constraint_state == "NOT VALID":
                print("  ALTER TABLE assets VALIDATE CONSTRAINT chk_assets_sha256_strict;")
            if not already_not_null:
                print("  ALTER TABLE assets ALTER COLUMN sha256 SET NOT NULL;")
            print()
            print("跑 --execute 真做")
            return 0

        # ── 2) execute 模式 · 真做 ───────────────────────────────────────
        print("→ 开始 finalize ...")

        if constraint_state == "NOT VALID":
            print("  → VALIDATE CONSTRAINT chk_assets_sha256_strict ...")
            await db.execute(
                text("ALTER TABLE assets VALIDATE CONSTRAINT chk_assets_sha256_strict;")
            )
            print("    ✓ done")

        if not already_not_null:
            print("  → ALTER COLUMN sha256 SET NOT NULL ...")
            await db.execute(
                text("ALTER TABLE assets ALTER COLUMN sha256 SET NOT NULL;")
            )
            print("    ✓ done")

        # 写 audit
        print("  → 写 audit event ...")
        await db.execute(
            text(
                """
                INSERT INTO audit_events (
                    tenant_id, project_id, actor_user_id, actor_kind,
                    action, target_kind, target_id, status, purpose,
                    ip, user_agent, metadata
                )
                SELECT
                    t.id, NULL, NULL, 'system',
                    'audit.constraint.validated', 'schema', NULL,
                    'success',
                    'finalize_sha256_strict completed · VALIDATE + NOT NULL applied',
                    NULL, NULL,
                    jsonb_build_object(
                        'script', 'scripts/finalize_sha256_strict.py',
                        'invariant', 'sha256 NOT NULL + CHECK VALID',
                        'actor_label', 'finalize_sha256_strict_2026-05-14'
                    )
                FROM tenants t WHERE t.slug = 'qide' LIMIT 1;
                """
            )
        )

        await db.commit()

        print()
        print("=== Finalize 完成 ===")
        print("  ✓ sha256 column NOT NULL")
        print("  ✓ chk_assets_sha256_strict VALIDATED")
        print("  ✓ 100% accuracy invariant DB-enforced")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
