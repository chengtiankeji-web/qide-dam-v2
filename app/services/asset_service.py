"""Business logic for assets — separated from API layer so MCP server can reuse it."""
from __future__ import annotations

import mimetypes
import uuid
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

async def register_presigned_upload(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    payload: PresignedUploadIn,
) -> tuple[Asset, str, dict]:
    """Create the Asset row in `uploading` state and return a presigned PUT URL.

    Caller (frontend / MCP client) PUTs the file directly to S3, then calls
    `confirm_upload` to flip status to ready and trigger Celery processing.
    """
    project = await _get_project(db, tenant_id=tenant_id, project_id=payload.project_id)
    tenant = await _get_tenant(db, tenant_id=tenant_id)

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
    status: str | None = "ready",
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Asset], int]:
    stmt = select(Asset).where(
        Asset.tenant_id == tenant_id,
        Asset.deleted_at.is_(None),
    )
    if project_id:
        stmt = stmt.where(Asset.project_id == project_id)
    if kind:
        stmt = stmt.where(Asset.kind == kind)
    if status:
        stmt = stmt.where(Asset.status == status)
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
    from datetime import datetime, timezone

    asset = await _get_asset_for_tenant(db, tenant_id=tenant_id, asset_id=asset_id)
    asset.deleted_at = datetime.now(timezone.utc)
    asset.status = "archived"
    await db.flush()


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
