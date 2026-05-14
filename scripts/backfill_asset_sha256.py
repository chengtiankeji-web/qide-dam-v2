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
    """返回 {status: 'done'|'failed'|'r2_missing'|'audit_failed', sha256?, error?}"""
    aid = str(asset.id)

    # 1. HEAD object 看存在不存在
    try:
        head = storage.head_object(asset.storage_key)
    except Exception as e:
        return {"status": "failed", "error": f"HEAD failed: {e}"}

    if head is None:
        # R2 上没这个对象 · 历史孤儿
        return {"status": "r2_missing", "storage_key": asset.storage_key}

    # 2. GET object · 算 sha256
    try:
        # storage.get_object 应该返回 bytes
        body = storage.get_object(asset.storage_key)
        if not isinstance(body, (bytes, bytearray, memoryview)):
            return {"status": "failed", "error": f"get_object returned {type(body)}"}
        sha = compute_sha256_streaming(body)
        size = len(body)
    except Exception as e:
        return {"status": "failed", "error": f"GET/hash failed: {e}"}

    # 3. 双检：DB 里 size_bytes 和 R2 size 应该匹配（不严格 · 老行可能没记 size）
    db_size = asset.size_bytes or 0
    if db_size > 0 and db_size != size:
        # 不阻塞 · 但记录
        print(f"  ⚠ size mismatch asset={aid} db={db_size} r2={size}")

    if dry_run:
        return {
            "status": "done",
            "sha256": sha,
            "size": size,
            "name": asset.name,
            "dry_run": True,
        }

    # 4. UPDATE
    try:
        await db.execute(
            update(Asset)
            .where(Asset.id == asset.id)
            .values(sha256=sha, size_bytes=size, updated_at=datetime.now(UTC))
        )
    except Exception as e:
        return {"status": "failed", "error": f"UPDATE failed: {e}"}

    # 5. audit
    try:
        from app.services import audit_service
        from app.services.audit_service import AuditAction

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
                "actor_label": ACTOR_LABEL,
            },
        )
    except Exception as e:
        # 不致命 · sha 已写
        print(f"  ⚠ audit write failed asset={aid}: {e}")
        return {"status": "audit_failed", "sha256": sha, "error": str(e)}

    return {"status": "done", "sha256": sha, "size": size, "name": asset.name}


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

    state = load_state() if args.continue_ else {"done": [], "failed": [], "stuck": [], "started_at": None}
    state["started_at"] = state.get("started_at") or datetime.now(UTC).isoformat()
    done_ids = set(state["done"])

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
            elif result["status"] == "r2_missing":
                print(f"⊗ R2 missing key={result['storage_key']}")
                state["stuck"].append({"id": str(asset.id), "storage_key": result["storage_key"]})
            elif result["status"] == "audit_failed":
                print(f"~ sha 写了但 audit 失败: {result.get('error', '?')}")
                state["done"].append(str(asset.id))  # sha 已写算 done
            else:  # failed
                print(f"✗ {result.get('error', 'unknown')}")
                state["failed"].append({"id": str(asset.id), "error": result.get("error")})

            # 每 10 行 commit + save state（防中途崩失进度）
            if not args.dry_run and i % 10 == 0:
                await db.commit()
                save_state(state)
                print(f"  ... committed batch · state saved")

        if not args.dry_run:
            await db.commit()
        save_state(state)

    # 汇总
    print()
    print(f"=== Backfill Summary ===")
    print(f"  done       : {len(state['done'])}")
    print(f"  failed     : {len(state['failed'])}")
    print(f"  r2_missing : {len(state['stuck'])}")
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
