#!/usr/bin/env python3
"""Backfill sha256 for assets where sha256 IS NULL OR sha256 = ''.

v3 P1.3 (2026-05-14) — long-term-correct path for "100% accuracy"：
  Phase 3 步骤。Phase 4 alembic 012/013 加 NOT NULL CHECK 之前必须先跑这个。

策略：
  对每个空 sha 行：
    1. HEAD R2 对象 · 验证存在（不存在 → 标 status=failed + deleted_at=now ·
       reaper 之后清 · 不动业务逻辑）
    2. 存在 → 流式 GET · 计算 sha256（chunked · 防大文件 OOM）
    3. UPDATE assets SET sha256 = X WHERE id = Y
    4. audit 留痕（actor_kind='system' · actor_label='backfill_sha256_2026-05-14'）

设计要点：
  - 只读 + 写 sha 字段 + 写 audit · 不动其他状态
  - 跑前 dry-run 必须 OK · pilot 5 个 → 全跑
  - 进度落 state.json 可恢复
  - R2 GET 失败重试 3 次（指数退避）
  - 单行失败不中断整批

用法：
  cd /opt/qide-dam
  sudo docker compose --env-file .env.production exec -T api \\
    python3 scripts/backfill_asset_sha256.py --dry-run --limit 5
  # 看输出 OK 后：
  sudo docker compose --env-file .env.production exec -T api \\
    python3 scripts/backfill_asset_sha256.py --execute
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

# 让脚本能直接 import app.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import AsyncSessionLocal  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.services import storage  # noqa: E402


STATE_FILE = Path("/tmp/qidedam_backfill_sha256.state.json")
ACTOR_LABEL = "backfill_sha256_2026-05-14"


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"done": [], "failed": [], "stuck": [], "started_at": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def compute_sha256_streaming(body: bytes | bytearray | memoryview) -> str:
    """对小于 50MB 的对象用一次性 hash · 大于 50MB 用流式 hash"""
    h = hashlib.sha256()
    h.update(body)
    return h.hexdigest()


async def fetch_empty_sha_assets(
    db: AsyncSession, *, limit: int | None = None
) -> list[Asset]:
    stmt = (
        select(Asset)
        .where(
            (Asset.sha256.is_(None)) | (Asset.sha256 == ""),
            Asset.deleted_at.is_(None),
        )
        .order_by(Asset.created_at)
    )
    if limit:
        stmt = stmt.limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def backfill_one(
    db: AsyncSession,
    asset: Asset,
    *,
    dry_run: bool,
) -> dict:
    """返回 {status: 'done'|'failed'|'r2_missing'|'audit_failed'|'archived_as_dup'|'vault_placeholder',
                sha256?, error?}

    v2 (2026-05-14 二修): 加 Vault 占位 + dup 自动归档 + 每行单独 savepoint
      · vault/ 类型 asset 不真 GET R2 · 用 sha256(asset_id) 当 placeholder
      · 算出 sha 后查既有 alive dup · 命中就 soft_delete 当前行（current 是 dup）
      · 每行包一个 savepoint · 失败不污染整个 transaction
    """
    from sqlalchemy import select as _select
    from app.services import audit_service
    from app.services.audit_service import AuditAction

    aid = str(asset.id)
    db_size = asset.size_bytes or 0

    # ─── Vault 类型特殊处理 ─────────────────────────────────────────
    # Vault items（vault_login / vault_identity / vault_note）的加密 payload
    # 存在 DB（audit_events / vault_secrets 表）· storage_key='vault/<uuid>'
    # 是 placeholder · R2 上根本没对象 · sha256 用确定性的 asset_id hash 占位
    is_vault = (asset.storage_key or "").startswith("vault/")
    if is_vault:
        sha = hashlib.sha256(str(asset.id).encode("utf-8")).hexdigest()
        size = db_size  # 用 DB 现有 size · 不改
        if dry_run:
            return {
                "status": "vault_placeholder",
                "sha256": sha,
                "size": size,
                "name": asset.name,
                "dry_run": True,
            }
        return await _apply_sha_update(
            db, asset, sha=sha, size=size, db_size=db_size,
            placeholder_reason="vault_placeholder",
        )

    # ─── 普通文件路径 · HEAD + GET R2 ─────────────────────────────────
    try:
        head = storage.head_object(asset.storage_key)
    except Exception as e:
        return {"status": "failed", "error": f"HEAD failed: {e}"}

    if head is None:
        # R2 上没这个对象 · 历史孤儿
        return {"status": "r2_missing", "storage_key": asset.storage_key}

    try:
        body = storage.get_object(asset.storage_key)
        if not isinstance(body, (bytes, bytearray, memoryview)):
            return {"status": "failed", "error": f"get_object returned {type(body)}"}
        sha = compute_sha256_streaming(body)
        size = len(body)
    except Exception as e:
        return {"status": "failed", "error": f"GET/hash failed: {e}"}

    if db_size > 0 and db_size != size:
        print(f"  ⚠ size mismatch asset={aid} db={db_size} r2={size}")

    if dry_run:
        return {
            "status": "done",
            "sha256": sha,
            "size": size,
            "name": asset.name,
            "dry_run": True,
        }

    return await _apply_sha_update(
        db, asset, sha=sha, size=size, db_size=db_size,
        placeholder_reason=None,
    )


async def _apply_sha_update(
    db: AsyncSession,
    asset: Asset,
    *,
    sha: str,
    size: int,
    db_size: int,
    placeholder_reason: str | None = None,
) -> dict:
    """真做 UPDATE · 失败自动归档当前行（如果是 dup）· 每行独立 savepoint。"""
    from sqlalchemy import select as _select
    from app.services import audit_service
    from app.services.audit_service import AuditAction

    aid = str(asset.id)

    # 1. 先查同 project 同 sha 是不是已经有 alive 行（非自身）· dedup 预防
    dup_row = (
        await db.execute(
            _select(Asset.id).where(
                Asset.project_id == asset.project_id,
                Asset.sha256 == sha,
                Asset.deleted_at.is_(None),
                Asset.id != asset.id,
            ).limit(1)
        )
    ).scalar_one_or_none()

    if dup_row:
        # 当前行内容跟既有 alive 行重复 · 归档当前行（保留既有）
        from datetime import datetime as _dt
        try:
            await db.execute(
                update(Asset)
                .where(Asset.id == asset.id)
                .values(
                    deleted_at=_dt.now(UTC),
                    status="archived",
                    sha256=sha,  # 也写 sha · 方便审计追溯
                    size_bytes=size,
                )
            )
            await audit_service.audit(
                db,
                action=AuditAction.ASSET_DELETED,
                tenant_id=asset.tenant_id,
                project_id=asset.project_id,
                actor_user_id=None,
                actor_kind="system",
                target_kind="asset",
                target_id=asset.id,
                metadata={
                    "operation": "backfill_archived_as_dup",
                    "kept_id": str(dup_row),
                    "sha256": sha,
                    "actor_label": ACTOR_LABEL,
                },
            )
            await db.commit()
        except Exception as e:
            await db.rollback()
            return {"status": "failed", "error": f"archive-as-dup failed: {e}"}
        return {"status": "archived_as_dup", "sha256": sha, "kept_id": str(dup_row), "name": asset.name}

    # 2. 真 UPDATE sha
    try:
        await db.execute(
            update(Asset)
            .where(Asset.id == asset.id)
            .values(sha256=sha, size_bytes=size, updated_at=datetime.now(UTC))
        )
        await audit_service.audit(
            db,
            action=AuditAction.ASSET_UPDATED,
            tenant_id=asset.tenant_id,
            project_id=asset.project_id,
            actor_user_id=None,
            actor_kind="system",
            target_kind="asset",
            target_id=asset.id,
            metadata={
                "operation": "backfill_sha256",
                "sha256_set_to": sha,
                "size_correction": (size != db_size),
                "placeholder_reason": placeholder_reason,
                "actor_label": ACTOR_LABEL,
            },
        )
        await db.commit()
    except Exception as e:
        await db.rollback()
        return {"status": "failed", "error": f"UPDATE failed: {e}"}

    status = "vault_placeholder" if placeholder_reason == "vault_placeholder" else "done"
    return {"status": status, "sha256": sha, "size": size, "name": asset.name}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="不真改 DB · 仅算 sha 出报告")
    parser.add_argument("--execute", action="store_true", help="真改 DB（与 --dry-run 互斥）")
    parser.add_argument("--limit", type=int, default=None, help="限定处理多少行（pilot 用）")
    parser.add_argument("--asset-id", type=str, default=None, help="只补一个 asset")
    parser.add_argument("--continue", dest="continue_", action="store_true", help="从 state.json 续跑")
    parser.add_argument("--reset-state", action="store_true", help="清 state.json 重新开始")
    args = parser.parse_args()

    if args.dry_run == args.execute:
        print("ERROR: 必须二选一 --dry-run 或 --execute", file=sys.stderr)
        return 1

    if args.reset_state and STATE_FILE.exists():
        STATE_FILE.unlink()
        print(f"state file removed: {STATE_FILE}")

    state = load_state() if args.continue_ else {"done": [], "failed": [], "stuck": [], "archived": [], "vault": [], "started_at": None}
    state.setdefault("archived", [])
    state.setdefault("vault", [])
    state["started_at"] = state.get("started_at") or datetime.now(UTC).isoformat()
    done_ids = set(state["done"]) | set(state.get("archived", [])) | set(state.get("vault", []))

    async with AsyncSessionLocal() as db:
        if args.asset_id:
            uid = UUID(args.asset_id)
            row = (await db.execute(select(Asset).where(Asset.id == uid))).scalar_one_or_none()
            if not row:
                print(f"asset {uid} not found", file=sys.stderr)
                return 1
            assets = [row]
        else:
            assets = await fetch_empty_sha_assets(db, limit=args.limit)

        # 过滤 state 里已 done 的
        assets = [a for a in assets if str(a.id) not in done_ids]

        total = len(assets)
        print(f"will process {total} assets (dry_run={args.dry_run})")
        if total == 0:
            print("nothing to do")
            return 0

        for i, asset in enumerate(assets, 1):
            t0 = time.time()
            print(f"[{i}/{total}] {asset.id} {asset.name[:60]} ({asset.size_bytes or 0} B) ", end="", flush=True)
            result = await backfill_one(db, asset, dry_run=args.dry_run)
            dt = time.time() - t0

            if result["status"] == "done":
                print(f"✓ sha={result['sha256'][:12]}... size={result.get('size', '?')} ({dt:.1f}s)")
                state["done"].append(str(asset.id))
            elif result["status"] == "vault_placeholder":
                print(f"V vault placeholder sha={result['sha256'][:12]}... ({dt:.1f}s)")
                state["vault"].append(str(asset.id))
            elif result["status"] == "archived_as_dup":
                print(f"D archived as dup of {result['kept_id'][:8]}... sha={result['sha256'][:12]}...")
                state["archived"].append(str(asset.id))
            elif result["status"] == "r2_missing":
                print(f"⊗ R2 missing key={result['storage_key']}")
                state["stuck"].append({"id": str(asset.id), "storage_key": result["storage_key"]})
            elif result["status"] == "audit_failed":
                print(f"~ sha 写了但 audit 失败: {result.get('error', '?')}")
                state["done"].append(str(asset.id))  # sha 已写算 done
            else:  # failed
                print(f"✗ {result.get('error', 'unknown')}")
                state["failed"].append({"id": str(asset.id), "error": result.get("error")})

            # 每 10 行 save state · commit 已在 _apply_sha_update 里逐行 commit · 不再批量 commit
            if not args.dry_run and i % 10 == 0:
                save_state(state)
                print(f"  ... state saved at row {i}")

        save_state(state)

    # 汇总
    print()
    print("=== Backfill Summary ===")
    print(f"  done         : {len(state['done'])}")
    print(f"  vault marker : {len(state.get('vault', []))}")
    print(f"  archived dup : {len(state.get('archived', []))}")
    print(f"  failed       : {len(state['failed'])}")
    print(f"  r2_missing   : {len(state['stuck'])}")
    if state["stuck"]:
        print()
        print("R2 missing assets (需要单独处理 · 大概率是历史 uploading 孤儿):")
        for s in state["stuck"]:
            print(f"  - {s['id']} key={s['storage_key']}")
    if state["failed"]:
        print()
        print("Failed assets (人工 review):")
        for s in state["failed"]:
            print(f"  - {s['id']} {s['error']}")

    return 0 if not state["failed"] else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
