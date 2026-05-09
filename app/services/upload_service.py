"""Multipart upload coordinator — for files >32 MiB.

Lifecycle:
    init    → create Asset(uploading) + S3 multipart upload + MultipartUpload row
    sign    → returns presigned UploadPart URL (one per part)
    complete→ finalize S3 multipart, mark Asset ready, enqueue processing
    abort   → drop S3 multipart + delete Asset
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.asset import Asset
from app.models.project import Project
from app.models.tenant import Tenant
from app.models.webhook import MultipartUpload
from app.schemas.upload import MultipartInitIn
from app.services import asset_service, storage


class DuplicateAssetError(Exception):
    """v3 phase 1.1 (2026-05-09): 同 project + 同 sha256 + 未删 = 重复
    抛给 endpoint 转 409 Conflict + existing_asset 元信息
    用户可以选 1) 跳过上传 2) skip_dedup=true 强制再传一次副本
    """

    def __init__(self, existing: Asset):
        self.existing = existing
        super().__init__(f"duplicate asset: {existing.name} (id={existing.id})")


async def _check_duplicate_sha256(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    sha256: str | None,
) -> Asset | None:
    """phase 1.1 dedup: 同项目下 sha256 命中且未删 = 重复
    sha256 为空（admin SPA 没传）跳过 · 仅 watcher / 显式传 sha256 时生效
    """
    if not sha256:
        return None
    found = (
        await db.execute(
            select(Asset).where(
                Asset.project_id == project_id,
                Asset.sha256 == sha256,
                Asset.deleted_at.is_(None),
                # 排除 status=uploading 自身（如果发生过 init 失败留下的孤儿）
                # 不过通常 sha256 在 init 阶段就传了 · 自身就是新建的 · 不会冲突
            ).limit(1)
        )
    ).scalar_one_or_none()
    return found


async def init_multipart(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    payload: MultipartInitIn,
    skip_dedup: bool = False,
) -> tuple[Asset, MultipartUpload]:
    project = (
        await db.execute(
            select(Project).where(
                Project.id == payload.project_id, Project.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if not project:
        raise ValueError("project not found")
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one()

    # v3 phase 1.1 (2026-05-09): sha256 dedup check
    if not skip_dedup:
        dup = await _check_duplicate_sha256(
            db, project_id=project.id, sha256=payload.sha256
        )
        if dup:
            raise DuplicateAssetError(dup)

    asset_id = uuid.uuid4()
    extension = asset_service.safe_extension(payload.filename, payload.mime_type)
    kind = asset_service.classify_kind(payload.mime_type, extension)
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

    # Create the S3 multipart upload session
    s3_upload_id = storage.initiate_multipart(
        storage_key=storage_key, content_type=payload.mime_type
    )

    mp = MultipartUpload(
        asset_id=asset.id,
        tenant_id=tenant_id,
        storage_key=storage_key,
        upload_id=s3_upload_id,
        expected_size=payload.size_bytes,
        parts_meta=[],
    )
    db.add(mp)
    await db.flush()
    return asset, mp


async def sign_part(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_id: uuid.UUID,
    part_number: int,
) -> str:
    mp = await _get_multipart(db, tenant_id=tenant_id, asset_id=asset_id)
    return storage.presign_upload_part(
        storage_key=mp.storage_key,
        upload_id=mp.upload_id,
        part_number=part_number,
        expires_in=3600,
    )


async def complete(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_id: uuid.UUID,
    parts: list[dict],
) -> Asset:
    """parts must be sorted ascending by PartNumber."""
    mp = await _get_multipart(db, tenant_id=tenant_id, asset_id=asset_id)
    storage.complete_multipart(
        storage_key=mp.storage_key, upload_id=mp.upload_id, parts=parts
    )
    mp.is_completed = True
    mp.parts_meta = parts

    asset = await asset_service.get_asset(db, tenant_id=tenant_id, asset_id=asset_id)
    head = storage.head_object(asset.storage_key)
    if head is not None:
        asset.size_bytes = int(head.get("ContentLength", asset.size_bytes))
    asset.status = "processing"
    if asset.acl == "public":
        asset.public_url = storage.public_url_for(asset.storage_key)
    await db.flush()
    return asset


async def abort(
    db: AsyncSession, *, tenant_id: uuid.UUID, asset_id: uuid.UUID
) -> None:
    mp = await _get_multipart(db, tenant_id=tenant_id, asset_id=asset_id)
    try:
        storage.abort_multipart(storage_key=mp.storage_key, upload_id=mp.upload_id)
    except Exception:  # noqa: BLE001 — abort is best-effort
        pass
    mp.aborted_at = datetime.now(UTC)
    asset = await asset_service.get_asset(db, tenant_id=tenant_id, asset_id=asset_id)
    asset.status = "failed"
    await db.flush()


async def _get_multipart(
    db: AsyncSession, *, tenant_id: uuid.UUID, asset_id: uuid.UUID
) -> MultipartUpload:
    mp = (
        await db.execute(
            select(MultipartUpload).where(
                MultipartUpload.asset_id == asset_id,
                MultipartUpload.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if not mp:
        raise ValueError("multipart session not found")
    if mp.is_completed:
        raise ValueError("multipart already completed")
    if mp.aborted_at is not None:
        raise ValueError("multipart aborted")
    return mp
