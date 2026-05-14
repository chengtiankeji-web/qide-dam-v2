"""Business logic for assets — separated from API layer so MCP server can reuse it."""
from __future__ import annotations

import mimetypes
import uuid
from datetime import UTC
from pathlib import PurePosixPath
from typing import Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.asset import Asset
from app.models.project import Project
from app.models.tenant import Tenant
from app.schemas.asset import PresignedUploadIn
from app.services import storage

# v3 P1.3 (2026-05-13): dedup strategy literal
DedupStrategy = Literal["reject", "link", "replicate"]

# kind classification by mime prefix
_KIND_BY_MIME_PREFIX: dict[str, str] = {
    "image/": "image",
    "video/": "video",
    "audio/": "audio",
}

_KIND_BY_EXT: dict[str, str] = {
    "pdf": "document", "doc": "document", "docx": "document",
    "xls": "document", "xlsx": "document", "ppt": "document", "pptx": "document",
    "txt": "document", "md": "document", "rtf": "document",
    "zip": "archive", "rar": "archive", "7z": "archive", "tar": "archive", "gz": "archive",
    "obj": "model3d", "fbx": "model3d", "gltf": "model3d", "glb": "model3d",
}


def classify_kind(mime_type: str, extension: str) -> str:
    for prefix, kind in _KIND_BY_MIME_PREFIX.items():
        if mime_type.startswith(prefix):
            return kind
    return _KIND_BY_EXT.get(extension.lower().lstrip("."), "other")


def safe_extension(filename: str, mime_type: str) -> str:
    ext = PurePosixPath(filename).suffix.lstrip(".").lower()
    if ext:
        return ext
    guess = mimetypes.guess_extension(mime_type) or ""
    return guess.lstrip(".").lower() or "bin"


# ----- presigned upload registration -----

class DuplicateAssetError(Exception):
    """v3 phase 1.1 (2026-05-09): 同 project + 同 sha256 + 未删 = 重复
    抛给 endpoint 转 409 Conflict + existing_asset 元信息
    """

    def __init__(self, existing: Asset):
        self.existing = existing
        super().__init__(f"duplicate asset: {existing.name} (id={existing.id})")


async def _find_duplicate_by_sha256(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    sha256: str | None,
    include_deleted: bool = False,
) -> Asset | None:
    """phase 1.1 dedup helper · sha256 空时跳过。

    v3 P1.3 (2026-05-13):
      - 调用方应通过 schema 校验保证 sha256 非空（防御性 None 跳过保留）
      - include_deleted=True · 也找 soft-deleted asset（用户在 DAM 删过的）
        这条让 watcher 知道"这 sha 是用户主动删的 · 不要再传"。
        优先返回 alive duplicate；alive 没有时才返 deleted dup。
    """
    if not sha256:
        return None

    # 先找 alive
    alive = (
        await db.execute(
            select(Asset).where(
                Asset.project_id == project_id,
                Asset.sha256 == sha256,
                Asset.deleted_at.is_(None),
            ).order_by(Asset.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if alive:
        return alive

    if not include_deleted:
        return None

    # 找 deleted（含 archived / soft-delete）· 返最新一个
    return (
        await db.execute(
            select(Asset).where(
                Asset.project_id == project_id,
                Asset.sha256 == sha256,
                Asset.deleted_at.is_not(None),
            ).order_by(Asset.deleted_at.desc()).limit(1)
        )
    ).scalar_one_or_none()


async def register_presigned_upload(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    payload: PresignedUploadIn,
    skip_dedup: bool = False,
    dedup_strategy: DedupStrategy = "link",
) -> tuple[Asset, str | None, dict, bool]:
    """Create the Asset row in `uploading` state and return a presigned PUT URL.

    Caller (frontend / MCP client) PUTs the file directly to S3, then calls
    `confirm_upload` to flip status to ready and trigger Celery processing.

    v3 P1.3 (2026-05-13): 三层防 dup
      1. schema 层：sha256 必填 + 64 hex（PresignedUploadIn pattern）
      2. app 层：先查 _find_duplicate_by_sha256 · 命中走 strategy 处理
      3. DB 层：partial unique index uq_assets_project_sha_alive (alembic 010)
         · 并发 race 时 IntegrityError → 回退查 dup 再处理

    dedup_strategy:
      - "link" (默认) · 命中 dup 时返回既有 asset · 跳 R2 PUT · 跳 confirm
                       · 给 watcher 用："这个 sha 已存在 我什么都不用做"
      - "reject" (legacy) · 命中 dup 时抛 DuplicateAssetError → endpoint 转 409
                            · 给 admin SPA Upload 用："请用户确认是否要副本"
      - "replicate" · skip dedup 检查 + 用户显式 ?skip_dedup=true · 落副本

    skip_dedup=True (legacy 参数) 自动设 strategy="replicate"。

    返回：(asset, upload_url, headers, deduplicated_bool)
      - deduplicated=True：asset 是既有的 · upload_url=None · 调用方跳上传
      - deduplicated=False：asset 新建在 uploading 状态 · upload_url 是 presigned R2 URL
    """
    # legacy compat
    if skip_dedup:
        dedup_strategy = "replicate"

    project = await _get_project(db, tenant_id=tenant_id, project_id=payload.project_id)
    tenant = await _get_tenant(db, tenant_id=tenant_id)

    # ── App 层 dedup 检查 ────────────────────────────────────────────
    # v3 P1.3 #3 watcher delete protection (2026-05-13 晚):
    # include_deleted=True · 也查 soft-deleted asset · 命中后返回 ·
    # 客户端（watcher）看到 existing_status='archived' 即可知"DAM 主动删过 · 别再传"。
    if dedup_strategy != "replicate":
        dup = await _find_duplicate_by_sha256(
            db,
            project_id=project.id,
            sha256=payload.sha256,
            include_deleted=(dedup_strategy == "link"),  # link 模式：兼顾删除的
        )
        if dup:
            if dedup_strategy == "link":
                # 此时 dup 可能是 alive 也可能是 deleted · status 字段透传到客户端
                # alive: status in (ready/processing/uploading) → watcher 标 done
                # deleted: status='archived' AND deleted_at != NULL → watcher 标 dam_deleted
                return dup, None, {}, True
            # strategy == "reject"（admin SPA 上传场景）
            # 历史行为：dup 只查 alive · 此处 dup 必是 alive
            raise DuplicateAssetError(dup)

    # ── 真上传路径 ───────────────────────────────────────────────────
    asset_id = uuid.uuid4()
    extension = safe_extension(payload.filename, payload.mime_type)
    kind = classify_kind(payload.mime_type, extension)
    storage_key = storage.build_storage_key(
        tenant_storage_prefix=tenant.storage_prefix,
        project_storage_prefix=project.storage_prefix,
        asset_id=asset_id,
        extension=extension,
    )

    asset = Asset(
        id=asset_id,
        tenant_id=tenant_id,
        project_id=project.id,
        name=payload.filename,
        sha256=payload.sha256,  # P1.3: schema 已保证非空 · 不再 `or ""`
        kind=kind,
        mime_type=payload.mime_type,
        extension=extension,
        size_bytes=payload.size_bytes,
        storage_key=storage_key,
        storage_bucket=settings.S3_BUCKET,
        public_url=None,
        status="uploading",
        source="upload",
        acl=payload.acl or project.default_acl,
        manual_tags=list(payload.manual_tags),
    )
    db.add(asset)

    # ── DB 层兜底：partial unique index 撞了说明 race · 转回 link 行为 ─
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        # 检查约束名（alembic 010 命名是 uq_assets_project_sha_alive）
        err_str = str(exc.orig) if exc.orig else str(exc)
        if "uq_assets_project_sha_alive" in err_str:
            # 并发场景：另一个调用刚 insert · 我们查既有就好
            dup = await _find_duplicate_by_sha256(
                db, project_id=project.id, sha256=payload.sha256
            )
            if dup:
                if dedup_strategy == "link":
                    return dup, None, {}, True
                raise DuplicateAssetError(dup) from exc
        # 其他 IntegrityError 透传 · 别吞了
        raise

    upload_url, headers = storage.presign_put(
        storage_key=storage_key, content_type=payload.mime_type, expires_in=900
    )
    return asset, upload_url, headers, False


async def confirm_upload(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_id: uuid.UUID,
) -> Asset:
    """Verify the object exists in S3, flip status -> processing, enqueue pipeline.

    v3 P1.3 (2026-05-13) P0 D3 修复：idempotent。
      之前 bug：重复 confirm 会双 bump usage + 双发 webhook + 双跑 Celery pipeline。
      现在：status 已经是 processing / ready 时早返回 · 不重复副作用。
      status=failed 时允许 re-arm（继续走流程 · 写一条 audit）·
      status=archived 时拒绝（不能复活已 soft-delete 的 asset）。
    """
    asset = await _get_asset_for_tenant(db, tenant_id=tenant_id, asset_id=asset_id)

    # P0 D3: 幂等早返回
    if asset.status in ("processing", "ready"):
        from app.core.logging import get_logger
        get_logger(__name__).info(
            "asset.confirm.idempotent_skip",
            asset_id=str(asset.id),
            status=asset.status,
        )
        return asset
    if asset.status == "archived":
        raise ValueError(
            f"asset {asset_id} is archived (deleted) · cannot confirm · "
            "restore from trash first"
        )
    if asset.status == "failed":
        # Re-arm path：让上传重试 · 写日志便于后续 audit
        from app.core.logging import get_logger
        get_logger(__name__).info(
            "asset.confirm.rearm_from_failed",
            asset_id=str(asset.id),
        )

    # 正常 uploading → processing 路径
    head = storage.head_object(asset.storage_key)
    if head is None:
        raise ValueError("Object not found in storage — did the upload finish?")
    actual_size = int(head.get("ContentLength", asset.size_bytes))
    asset.size_bytes = actual_size

    asset.status = "processing"
    if asset.acl == "public":
        asset.public_url = storage.public_url_for(asset.storage_key)
    await db.flush()

    # 2026-04-29 fix: bump tenant usage counters (storage_bytes_total +=
    # actual_size, upload_bytes += actual_size, new_asset_count += 1)
    # Without this the Dashboard's 30-day summary stays at 0 forever.
    try:
        from app.services import usage_service
        await usage_service.bump(
            db,
            tenant_id=tenant_id,
            storage_delta_bytes=actual_size,
            upload_bytes=actual_size,
            new_asset_count=1,
        )
    except Exception as e:  # noqa: BLE001
        from app.core.logging import get_logger
        get_logger(__name__).warning("asset.confirm.usage_bump_failed", error=str(e))

    # Sprint 2: enqueue the post-processing pipeline (image/video/document → ai)
    try:
        from app.workers.tasks_pipeline import process_pipeline
        process_pipeline.delay(str(asset.id))
    except Exception as e:  # noqa: BLE001 — Celery may not be reachable in tests
        from app.core.logging import get_logger
        get_logger(__name__).warning("asset.confirm.celery_unavailable", error=str(e))
        asset.status = "ready"

    # Fire the asset.uploaded webhook
    try:
        from app.services.webhook_service import enqueue_event
        await enqueue_event(
            db,
            tenant_id=tenant_id,
            event_type="asset.uploaded",
            project_id=asset.project_id,
            payload={
                "asset_id": str(asset.id),
                "name": asset.name,
                "kind": asset.kind,
                "size_bytes": asset.size_bytes,
                "storage_key": asset.storage_key,
            },
        )
    except Exception:  # noqa: BLE001
        pass

    return asset


# ─── inline-content upload helper · v3 P1.3 phase 2 (2026-05-14) ─────
#
# 设计目的：把"Claude / pipeline 生成的内容直接落 DAM"这条路径**单点化** ·
# 之前 consolidate.apply 和 Smart Intake 各自实现了 register → PUT → confirm
# 三步，每个地方都没正确处理"PUT 失败 → Asset 行留孤儿"的清理。
# 这个 helper 把整条流程做成**原子**（失败回滚 soft_delete 占位行）·
# 所有"内容已生成 · 入 DAM"的场景都应该走这里 · 不许再各自重写。


class InlineUploadError(RuntimeError):
    """v3 P1.3 phase 2 · upload_inline_content 抛 · 内含已清理的孤儿 asset_id 用于调试"""

    def __init__(self, message: str, *, asset_id: uuid.UUID | None = None) -> None:
        super().__init__(message)
        self.cleaned_asset_id = asset_id


async def upload_inline_content(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    filename: str,
    content: bytes,
    mime_type: str = "application/octet-stream",
    manual_tags: list[str] | None = None,
    acl: str | None = None,
    dedup_strategy: DedupStrategy = "link",
) -> tuple[Asset, bool]:
    """原子上传"已在内存里的字节流"到 DAM · register + PUT + confirm + 失败清理 一气呵成。

    场景：
      - consolidate.apply 写 memory.md
      - Smart Intake 自动生成的 intake_report.md
      - 未来任何 LLM/Pipeline 产出 → DAM 入库

    返回：(asset, deduplicated)
      - deduplicated=True 时 asset 是既有的 · R2 PUT 跳过（dedup_strategy='link' 时）
      - deduplicated=False 时 asset 已 confirmed · 在 R2 上有真对象 + status='processing'/'ready'

    异常路径（任何失败都保证 DAM 无孤儿 Asset 行）：
      - R2 PUT 非 2xx → soft_delete 占位行 + 抛 InlineUploadError
      - confirm 失败 → soft_delete + 抛 InlineUploadError
      - dedup_strategy='reject' 命中既有 → 抛 DuplicateAssetError（不创建占位）

    调用方拿到异常后无需手动清理 · 此 helper 自己负责。
    """
    import hashlib as _hashlib

    sha = _hashlib.sha256(content).hexdigest()
    payload = PresignedUploadIn(
        project_id=project_id,
        filename=filename,
        mime_type=mime_type,
        size_bytes=len(content),
        sha256=sha,
        acl=acl,
        manual_tags=list(manual_tags or []),
    )

    asset, upload_url, headers, deduplicated = await register_presigned_upload(
        db,
        tenant_id=tenant_id,
        payload=payload,
        dedup_strategy=dedup_strategy,
    )

    if deduplicated:
        # 既有 sha 命中 · 不需要 PUT 也不需要 confirm
        return asset, True

    asset_id = asset.id  # 捕获 · 失败路径要清理

    # ── PUT to R2 ────────────────────────────────────────────────────
    try:
        import requests as _req
        resp = _req.put(
            upload_url,
            data=content,
            headers={"Content-Type": mime_type, **(headers or {})},
            timeout=60,
        )
        if resp.status_code not in (200, 201, 204):
            raise InlineUploadError(
                f"R2 PUT failed: HTTP {resp.status_code} body={resp.text[:200]!r}",
                asset_id=asset_id,
            )
    except InlineUploadError:
        # 已是我们的异常 · 走清理 + reraise
        await _cleanup_orphan_inline_upload(db, tenant_id=tenant_id, asset_id=asset_id)
        raise
    except Exception as exc:
        await _cleanup_orphan_inline_upload(db, tenant_id=tenant_id, asset_id=asset_id)
        raise InlineUploadError(
            f"R2 PUT exception: {exc!s}", asset_id=asset_id
        ) from exc

    # ── confirm（flip uploading → processing + 启 Celery + bump usage） ──
    try:
        await confirm_upload(db, tenant_id=tenant_id, asset_id=asset_id)
    except Exception as exc:
        # confirm 失败 · R2 对象已存在但 DB 行状态不对 · 还是 soft_delete 标记 ·
        # 之后 reaper 会扫"deleted_at + R2 对象存在" → record r2_orphan 走重试
        await _cleanup_orphan_inline_upload(db, tenant_id=tenant_id, asset_id=asset_id)
        raise InlineUploadError(
            f"confirm_upload failed: {exc!s}", asset_id=asset_id
        ) from exc

    return asset, False


async def _cleanup_orphan_inline_upload(
    db: AsyncSession, *, tenant_id: uuid.UUID, asset_id: uuid.UUID
) -> None:
    """upload_inline_content 失败时清占位 Asset · best-effort 不抛新异常"""
    try:
        await soft_delete_asset(db, tenant_id=tenant_id, asset_id=asset_id)
    except Exception as exc:  # noqa: BLE001
        from app.core.logging import get_logger
        get_logger(__name__).warning(
            "inline_upload.cleanup_failed",
            asset_id=str(asset_id),
            error=str(exc),
        )


# ----- list & search (Sprint 1: basic; Sprint 3 adds vector search) -----

async def list_assets(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    kind: str | None = None,
    status: str | None = None,  # 2026-04-29 fix: 默认显示全部 status
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
    # v3 P0-3: callers that should never see secret-level assets pass
    # exclude_secret=True. MCP tools and the AI Gateway always do; the
    # admin Assets page allows users to see (but not reveal) secret rows.
    exclude_secret: bool = False,
    # 2026-05-08 phase 1 trash: True 时只列出回收站（已 soft delete 的）
    # · False（默认）只列出活资产 · 永远不同时显示两类
    show_trashed: bool = False,
    # 2026-05-10 phase 1.2 folder filter:
    #   None      — 不按 folder 过滤（显示项目下所有 folder 的 + 根目录的）
    #   "__root__" — 只显示根目录（folder_id IS NULL 的）
    #   uuid       — 只显示该 folder 下的
    folder_filter: str | uuid.UUID | None = None,
) -> tuple[list[Asset], int]:
    stmt = select(Asset).where(
        Asset.tenant_id == tenant_id,
        Asset.deleted_at.is_not(None) if show_trashed else Asset.deleted_at.is_(None),
    )
    if project_id:
        stmt = stmt.where(Asset.project_id == project_id)
    if folder_filter is not None:
        if isinstance(folder_filter, str) and folder_filter == "__root__":
            stmt = stmt.where(Asset.folder_id.is_(None))
        else:
            stmt = stmt.where(Asset.folder_id == folder_filter)
    if kind:
        stmt = stmt.where(Asset.kind == kind)
    if status:
        stmt = stmt.where(Asset.status == status)
    if exclude_secret:
        # Vault-kind assets are always sensitivity=secret; this filter
        # also catches any future asset that gets manually marked secret.
        stmt = stmt.where(Asset.sensitivity_level != "secret")
        stmt = stmt.where(
            ~Asset.kind.in_(("vault_login", "vault_identity", "vault_note"))
        )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                Asset.name.ilike(like),
                Asset.description.ilike(like),
                func.array_to_string(Asset.manual_tags, " ").ilike(like),
                func.array_to_string(Asset.auto_tags, " ").ilike(like),
            )
        )

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(Asset.created_at.desc()).limit(page_size).offset((page - 1) * page_size)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows), int(total)


async def get_asset(
    db: AsyncSession, *, tenant_id: uuid.UUID, asset_id: uuid.UUID
) -> Asset:
    return await _get_asset_for_tenant(db, tenant_id=tenant_id, asset_id=asset_id)


async def soft_delete_asset(
    db: AsyncSession, *, tenant_id: uuid.UUID, asset_id: uuid.UUID
) -> None:
    from datetime import datetime

    asset = await _get_asset_for_tenant(db, tenant_id=tenant_id, asset_id=asset_id)
    asset.deleted_at = datetime.now(UTC)
    asset.status = "archived"
    await db.flush()


# ─── phase 1 (2026-05-08): 回收站 / 永久删除 ─────────────────────────


async def restore_asset(
    db: AsyncSession, *, tenant_id: uuid.UUID, asset_id: uuid.UUID
) -> Asset:
    """从回收站恢复 · 清 deleted_at + 把 status 翻回 ready"""
    asset = await _get_asset_for_tenant_include_trashed(
        db, tenant_id=tenant_id, asset_id=asset_id
    )
    if asset.deleted_at is None:
        raise ValueError("asset not in trash")
    asset.deleted_at = None
    asset.status = "ready"
    await db.flush()
    return asset


async def hard_delete_asset(
    db: AsyncSession, *, tenant_id: uuid.UUID, asset_id: uuid.UUID
) -> dict:
    """永久删除 · 从 R2 删 storage_key + 缩略图 + DB 行（cascade 到 versions / collection_assets 等）

    注：只对已 soft-delete 过的资产生效（要求 deleted_at IS NOT NULL）
    防止误调把活资产直接抹掉。
    """
    from app.services import storage

    asset = await _get_asset_for_tenant_include_trashed(
        db, tenant_id=tenant_id, asset_id=asset_id
    )
    if asset.deleted_at is None:
        raise ValueError("can only hard-delete assets already in trash · soft-delete first")

    # 收集所有要从 R2 删的对象 keys
    keys_to_delete = []
    if asset.storage_key:
        keys_to_delete.append(asset.storage_key)
    if asset.thumbnails and isinstance(asset.thumbnails, dict):
        for v in asset.thumbnails.values():
            if v:
                keys_to_delete.append(v)

    deleted_keys = []
    failed_keys = []
    for k in keys_to_delete:
        try:
            storage.delete_object(k)
            deleted_keys.append(k)
        except Exception as exc:  # noqa: BLE001
            failed_keys.append({"key": k, "error": str(exc)})

    # v3 P1.3 D7 修复：R2 删失败的 key 录入 r2_orphans 表 ·
    # 之前 bug：r2_failed 返回但 DB 行已删 → R2 对象成永久孤儿（不被任何表引用 + 计费 forever）
    # 现在：每个失败的 storage_key 入 r2_orphans 表 + retry_r2_orphans 每天 backoff 重试
    if failed_keys:
        try:
            from app.workers.tasks_cleanup import record_r2_orphan
            for fk in failed_keys:
                await record_r2_orphan(
                    db,
                    tenant_id=asset.tenant_id,
                    project_id=asset.project_id,
                    origin_asset_id=asset.id,
                    storage_key=fk["key"],
                    storage_bucket=asset.storage_bucket or "",
                    error=fk["error"],
                )
        except Exception as exc:  # noqa: BLE001
            from app.core.logging import get_logger
            get_logger(__name__).warning(
                "record_r2_orphan failed (R2 obj still orphan but not tracked)",
                error=str(exc),
            )

    # DB 行删除 · cascade 配置由模型 / FK 决定（见 alembic 001-004）
    await db.delete(asset)
    await db.flush()

    return {
        "asset_id": str(asset_id),
        "r2_deleted": deleted_keys,
        "r2_failed": failed_keys,
    }


async def empty_trash(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
) -> dict:
    """清空当前租户（或租户内某项目）的回收站。返回 hard-delete 摘要。"""
    from sqlalchemy import select as _s

    stmt = _s(Asset).where(
        Asset.tenant_id == tenant_id,
        Asset.deleted_at.is_not(None),
    )
    if project_id:
        stmt = stmt.where(Asset.project_id == project_id)

    rows = (await db.execute(stmt)).scalars().all()
    count = 0
    failed = []
    for a in rows:
        try:
            await hard_delete_asset(db, tenant_id=tenant_id, asset_id=a.id)
            count += 1
        except Exception as exc:  # noqa: BLE001
            failed.append({"asset_id": str(a.id), "error": str(exc)})
    return {"deleted_count": count, "failed": failed}


async def dedup_by_name(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    folder_scoped: bool = True,
    dry_run: bool = True,
) -> dict:
    """v3 P1.3 #2 (2026-05-13 晚) · 按 (project_id, folder_id, name) 去重。

    用例：Memory 文件 Sam 多次编辑同名 CLAUDE.md / xiangyue-shunde.md ·
    每次内容不同（sha 不同）→ alembic 010 不去重 · 累积版本。
    本端点按 name 分组 · 保留 updated_at 最新一行 · 其他 soft-delete。

    folder_scoped:
      True (默认) = 同 folder 下重名才算 dup（更严）
      False = 同 project 下重名就算 dup（极宽 · 会误删跨 folder 同名文件）

    幂等 · 跑 N 次结果一致 · audit 留痕。

    v3 P1.3 phase 5 (2026-05-14) 重写：
      · 之前版本：load 整个 tenant 所有 alive asset 入 Python · 大租户 OOM 风险
      · 现在版本：SQL ROW_NUMBER 直接拿 dup 组 · 内存只放真要处理的行
      · 排序加 Asset.id tie-breaker · 解 dry-run 不幂等 bug（两次跑结果不同）
    """
    from sqlalchemy import tuple_

    from app.services import audit_service
    from app.services.audit_service import AuditAction

    # ── Step 1: SQL GROUP BY 找 dup 组（只取 HAVING COUNT > 1） ──────────
    # 注：folder_id 可能 NULL · 用 (project_id, COALESCE_INDEX, name) 分组。
    # NULL 在 GROUP BY 里是 "等于自己" 的（PG 行为）· 所以 NULL folder_id 的同名文件正确归一组。
    grouping_cols = [Asset.project_id, Asset.name]
    if folder_scoped:
        grouping_cols.insert(1, Asset.folder_id)

    base_where = [
        Asset.tenant_id == tenant_id,
        Asset.deleted_at.is_(None),
    ]
    if project_id:
        base_where.append(Asset.project_id == project_id)

    group_count_stmt = (
        select(*grouping_cols, func.count().label("n"))
        .where(*base_where)
        .group_by(*grouping_cols)
        .having(func.count() > 1)
    )
    dup_group_rows = (await db.execute(group_count_stmt)).all()

    if not dup_group_rows:
        return {
            "project_id": str(project_id) if project_id else None,
            "folder_scoped": folder_scoped,
            "dry_run": dry_run,
            "dup_groups": 0,
            "archived_count": 0,
            "sample": [],
        }

    # ── Step 2: 只 fetch 这些组里的所有 asset ────────────────────────────
    # 用 (project_id, [folder_id,] name) tuple 一次性 IN 查询
    if folder_scoped:
        keys = [(r[0], r[1], r[2]) for r in dup_group_rows]  # (project_id, folder_id, name)
        member_filter = tuple_(Asset.project_id, Asset.folder_id, Asset.name).in_(keys)
    else:
        keys = [(r[0], r[1]) for r in dup_group_rows]  # (project_id, name)
        member_filter = tuple_(Asset.project_id, Asset.name).in_(keys)

    # tie-breaker · v3 P1.3 phase 5 修：order_by 加 Asset.id 保证幂等
    # 否则同 updated_at 时 ROW_NUMBER 不稳定 · 两次 dry-run 给出不同的 kept_id
    member_rows = (
        await db.execute(
            select(Asset)
            .where(*base_where, member_filter)
            .order_by(
                Asset.project_id,
                Asset.folder_id,
                Asset.name,
                Asset.updated_at.desc(),
                Asset.id,  # tie-breaker
            )
        )
    ).scalars().all()

    # ── Step 3: Python 端按组聚合 + soft-delete 非最新 ────────────────────
    def _key(a: Asset):
        return (a.project_id, a.folder_id if folder_scoped else None, a.name)

    groups: dict = {}
    for a in member_rows:
        groups.setdefault(_key(a), []).append(a)

    archived_count = 0
    sample: list[dict] = []
    dt_now = None

    for (proj_id, folder_id, name), assets in groups.items():
        if len(assets) < 2:
            # GROUP BY 说有 dup · 但 member 查回来只有 1 行 · 可能并发已 soft-delete · 跳过
            continue
        kept = assets[0]  # order_by 已保证 updated_at desc + id 是稳定 first
        to_archive = assets[1:]
        if not dry_run:
            if dt_now is None:
                from datetime import datetime as _dt
                dt_now = _dt.now(UTC)
            for a in to_archive:
                a.deleted_at = dt_now
                a.status = "archived"
                await audit_service.audit(
                    db,
                    action=AuditAction.ASSET_DELETED,
                    tenant_id=tenant_id,
                    project_id=proj_id,
                    actor_user_id=None,
                    actor_kind="system",
                    target_kind="asset",
                    target_id=a.id,
                    metadata={
                        "reason": "duplicate_name",
                        "kept_id": str(kept.id),
                        "kept_updated_at": kept.updated_at.isoformat() if kept.updated_at else None,
                        "name": name,
                        "folder_id": str(folder_id) if folder_id else None,
                        "folder_scoped": folder_scoped,
                        "actor_label": "dedup_by_name_endpoint",
                    },
                )
        archived_count += len(to_archive)
        if len(sample) < 20:
            sample.append({
                "project_id": str(proj_id),
                "folder_id": str(folder_id) if folder_id else None,
                "name": name,
                "kept_id": str(kept.id),
                "kept_size": kept.size_bytes,
                "kept_updated_at": kept.updated_at.isoformat() if kept.updated_at else None,
                "archived_ids": [str(a.id) for a in to_archive],
                "archived_count": len(to_archive),
            })

    if not dry_run:
        await db.flush()

    return {
        "project_id": str(project_id) if project_id else None,
        "folder_scoped": folder_scoped,
        "dry_run": dry_run,
        "dup_groups": len(dup_group_rows),
        "archived_count": archived_count,
        "sample": sample,
    }


async def dedup_by_sha256(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    dry_run: bool = True,
) -> dict:
    """v3 P1.3 (2026-05-13) · 一次性清同 project 同 sha256 重复 asset。

    - 找出每个 (project_id, sha256) 多于 1 行的组 · sha256<>'' · 未删
    - 保留 created_at 最新的一行 · 其他 soft-delete（status='archived'）
    - dry_run=True 只报告不动数据 · dry_run=False 真改

    每个 archive 操作写一条 audit event（actor=system / kind=migration_dedup）
    跑完返回报告 · 调用方可决定是否再跑一次。

    幂等：跑 N 次结果一致（第二次找到 0 dup group）。
    """
    from app.services import audit_service
    from app.services.audit_service import AuditAction

    # 找 dup groups · 限 tenant · 可选 project filter
    where_clauses = [
        Asset.tenant_id == tenant_id,
        Asset.deleted_at.is_(None),
        Asset.sha256 != "",
    ]
    if project_id:
        where_clauses.append(Asset.project_id == project_id)

    # 拉所有 active+sha256 行 · 按 (project_id, sha256) 分组手工 dedup
    rows = (
        await db.execute(
            select(Asset).where(*where_clauses)
            .order_by(Asset.project_id, Asset.sha256, Asset.created_at.desc())
        )
    ).scalars().all()

    groups: dict[tuple[uuid.UUID, str], list[Asset]] = {}
    for a in rows:
        key = (a.project_id, a.sha256)
        groups.setdefault(key, []).append(a)

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    archived_count = 0
    sample: list[dict] = []

    for (proj_id, sha), assets in dup_groups.items():
        # rows 已按 created_at DESC 排 · 第一个保留 · 其他 archive
        kept = assets[0]
        to_archive = assets[1:]
        if not dry_run:
            from datetime import datetime as _dt
            now = _dt.now(UTC)
            for a in to_archive:
                a.deleted_at = now
                a.status = "archived"
                # 写 audit · target_id=a.id · kept_id 进 metadata
                # audit_service.audit() 接受字段见 audit_service.py:audit()
                # · actor_label / severity 不是顶层参数 · 放 metadata 里
                await audit_service.audit(
                    db,
                    action=AuditAction.ASSET_DELETED,
                    tenant_id=tenant_id,
                    project_id=proj_id,
                    actor_user_id=None,
                    actor_kind="system",
                    target_kind="asset",
                    target_id=a.id,
                    metadata={
                        "reason": "duplicate_sha256",
                        "kept_id": str(kept.id),
                        "sha256": sha,
                        "dry_run": False,
                        "actor_label": "dedup_by_sha256_endpoint",
                    },
                )
        archived_count += len(to_archive)
        if len(sample) < 20:
            sample.append({
                "project_id": str(proj_id),
                "sha256": sha[:16] + "...",  # 截断不暴露完整 sha
                "kept_id": str(kept.id),
                "kept_name": kept.name,
                "archived_ids": [str(a.id) for a in to_archive],
                "archived_count": len(to_archive),
            })

    if not dry_run:
        await db.flush()

    return {
        "project_id": str(project_id) if project_id else None,
        "dry_run": dry_run,
        "dup_groups": len(dup_groups),
        "archived_count": archived_count,
        "sample": sample,
    }


async def purge_old_trashed(
    db: AsyncSession,
    *,
    older_than_days: int = 15,
) -> dict:
    """系统 cron 每天调一次（见 tasks_cleanup.py / celery beat）

    硬删 deleted_at < now - N 天 的资产 · 跨所有 tenant · 默认 15 天。
    """
    from datetime import datetime, timedelta

    from sqlalchemy import select as _s

    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    rows = (
        await db.execute(
            _s(Asset).where(
                Asset.deleted_at.is_not(None),
                Asset.deleted_at < cutoff,
            )
        )
    ).scalars().all()

    count = 0
    failed = []
    for a in rows:
        try:
            await hard_delete_asset(db, tenant_id=a.tenant_id, asset_id=a.id)
            count += 1
        except Exception as exc:  # noqa: BLE001
            failed.append({"asset_id": str(a.id), "error": str(exc)})
    await db.commit()
    return {
        "purged_count": count,
        "failed": failed,
        "cutoff": cutoff.isoformat(),
    }


# ----- internals -----

async def _get_project(
    db: AsyncSession, *, tenant_id: uuid.UUID, project_id: uuid.UUID
) -> Project:
    project = (
        await db.execute(
            select(Project).where(
                and_(Project.id == project_id, Project.tenant_id == tenant_id)
            )
        )
    ).scalar_one_or_none()
    if not project:
        raise ValueError(f"project {project_id} not found in tenant")
    return project


async def _get_tenant(db: AsyncSession, *, tenant_id: uuid.UUID) -> Tenant:
    tenant = (
        await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if not tenant:
        raise ValueError(f"tenant {tenant_id} not found")
    return tenant


async def _get_asset_for_tenant(
    db: AsyncSession, *, tenant_id: uuid.UUID, asset_id: uuid.UUID
) -> Asset:
    """获取活资产 · 不含回收站。回收站操作走下面那个变体。"""
    asset = (
        await db.execute(
            select(Asset).where(
                and_(
                    Asset.id == asset_id,
                    Asset.tenant_id == tenant_id,
                    Asset.deleted_at.is_(None),
                )
            )
        )
    ).scalar_one_or_none()
    if not asset:
        raise ValueError(f"asset {asset_id} not found in tenant")
    return asset


async def _get_asset_for_tenant_include_trashed(
    db: AsyncSession, *, tenant_id: uuid.UUID, asset_id: uuid.UUID
) -> Asset:
    """v3 phase 1: restore / hard_delete 用 · 包括回收站资产"""
    asset = (
        await db.execute(
            select(Asset).where(
                and_(Asset.id == asset_id, Asset.tenant_id == tenant_id)
            )
        )
    ).scalar_one_or_none()
    if not asset:
        raise ValueError(f"asset {asset_id} not found in tenant")
    return asset
