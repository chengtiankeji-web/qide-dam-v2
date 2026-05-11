"""Business logic for assets — separated from API layer so MCP server can reuse it."""
from __future__ import annotations

import mimetypes
import uuid
from datetime import UTC
from pathlib import PurePosixPath

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.asset import Asset
from app.models.project import Project
from app.models.tenant import Tenant
from app.schemas.asset import PresignedUploadIn
from app.services import storage

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
) -> Asset | None:
    """phase 1.1 dedup helper · sha256 空时跳过（兼容老 client / watcher 必传）"""
    if not sha256:
        return None
    return (
        await db.execute(
            select(Asset).where(
                Asset.project_id == project_id,
                Asset.sha256 == sha256,
                Asset.deleted_at.is_(None),
            ).limit(1)
        )
    ).scalar_one_or_none()


async def register_presigned_upload(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    payload: PresignedUploadIn,
    skip_dedup: bool = False,
) -> tuple[Asset, str, dict]:
    """Create the Asset row in `uploading` state and return a presigned PUT URL.

    Caller (frontend / MCP client) PUTs the file directly to S3, then calls
    `confirm_upload` to flip status to ready and trigger Celery processing.

    v3 phase 1.1: 检查同 project sha256 重复 · skip_dedup=true 跳过（用户要副本）
    """
    project = await _get_project(db, tenant_id=tenant_id, project_id=payload.project_id)
    tenant = await _get_tenant(db, tenant_id=tenant_id)

    if not skip_dedup:
        dup = await _find_duplicate_by_sha256(
            db, project_id=project.id, sha256=payload.sha256
        )
        if dup:
            raise DuplicateAssetError(dup)

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
        sha256=payload.sha256 or "",
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
    await db.flush()

    upload_url, headers = storage.presign_put(
        storage_key=storage_key, content_type=payload.mime_type, expires_in=900
    )
    return asset, upload_url, headers


async def confirm_upload(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_id: uuid.UUID,
) -> Asset:
    """Verify the object exists in S3, flip status -> processing, enqueue pipeline."""
    asset = await _get_asset_for_tenant(db, tenant_id=tenant_id, asset_id=asset_id)
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
