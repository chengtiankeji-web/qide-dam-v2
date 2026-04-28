"""MCP (Model Context Protocol) Server — full v2 toolkit.

Sprints 1-2 ship 12 tools; Sprint 3 adds AI tools (search_similar, alt_text);
Sprint 4 adds collection / workflow tools.

Two transports:
- stdio   →  `python -m app.mcp.server --api-key dam_test_xxx`
- HTTP/SSE→  `python -m app.mcp.http_server`  (port 8001)

Authentication: the DAM API key resolves via:
  1. contextvar set by HTTP middleware (`app.mcp.http_server`)
  2. env var `DAM_API_KEY`
  3. `--api-key` CLI flag (writes into env var before mcp.run)
"""
from __future__ import annotations

import argparse
import os
import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select

from app.core.logging import configure_logging, get_logger
from app.core.security import hash_api_key
from app.db.session import AsyncSessionLocal
from app.models.api_key import ApiKey
from app.models.project import Project
from app.schemas.asset import PresignedUploadIn
from app.services import asset_service

configure_logging()
logger = get_logger("mcp")

mcp = FastMCP("qide-dam")


# ----- key resolution (HTTP server overrides this) -----

def _get_runtime_api_key() -> str:
    key = os.getenv("DAM_API_KEY", "").strip()
    if not key:
        raise RuntimeError("DAM_API_KEY not set")
    return key


async def _resolve_principal(db) -> ApiKey:
    raw = _get_runtime_api_key()
    digest = hash_api_key(raw)
    api_key = (
        await db.execute(
            select(ApiKey).where(ApiKey.key_hash == digest, ApiKey.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if not api_key:
        raise PermissionError("Invalid or revoked DAM API key")
    return api_key


# ============================================================
# 1. list_assets — paginated list with filters
# ============================================================
@mcp.tool()
async def list_assets(
    project_id: str | None = None,
    kind: str | None = None,
    status: str | None = "ready",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """List assets in the tenant. Filters: project_id, kind, status."""
    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        proj_uuid = uuid.UUID(project_id) if project_id else None
        if api_key.project_id and proj_uuid and api_key.project_id != proj_uuid:
            raise PermissionError("API key scoped to a different project")
        items, total = await asset_service.list_assets(
            db,
            tenant_id=api_key.tenant_id,
            project_id=proj_uuid or api_key.project_id,
            kind=kind,
            status=status,
            page=page,
            page_size=page_size,
        )
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [_summary(a) for a in items],
        }


# ============================================================
# 2. search_assets — keyword search across name/description/tags
# ============================================================
@mcp.tool()
async def search_assets(q: str, page_size: int = 20) -> dict[str, Any]:
    """Keyword search. Sprint 3 adds vector / semantic search via search_similar."""
    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        items, total = await asset_service.list_assets(
            db,
            tenant_id=api_key.tenant_id,
            project_id=api_key.project_id,
            q=q,
            page=1,
            page_size=page_size,
        )
        return {"query": q, "total": total, "items": [_summary(a) for a in items]}


# ============================================================
# 3. get_asset — full metadata for one asset
# ============================================================
@mcp.tool()
async def get_asset(asset_id: str) -> dict[str, Any]:
    """Fetch full metadata for a single asset by UUID."""
    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        asset = await asset_service.get_asset(
            db, tenant_id=api_key.tenant_id, asset_id=uuid.UUID(asset_id)
        )
        if api_key.project_id and asset.project_id != api_key.project_id:
            raise PermissionError("API key cannot access this project")
        return _full(asset)


# ============================================================
# 4. register_upload — get a presigned PUT URL (small files <32MB)
# ============================================================
@mcp.tool()
async def register_upload(
    project_id: str,
    filename: str,
    mime_type: str,
    size_bytes: int,
    sha256: str | None = None,
    acl: str = "project",
    manual_tags: list[str] | None = None,
) -> dict[str, Any]:
    """Register a new asset and get a presigned PUT URL (files up to ~5GB).

    Caller PUTs the bytes to upload_url then calls confirm_upload(asset_id).
    For files >32MB prefer multipart_init for parallelism.
    """
    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        proj_uuid = uuid.UUID(project_id)
        if api_key.project_id and api_key.project_id != proj_uuid:
            raise PermissionError("API key scoped to a different project")
        payload = PresignedUploadIn(
            project_id=proj_uuid,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            sha256=sha256,
            acl=acl,
            manual_tags=manual_tags or [],
        )
        asset, url, headers = await asset_service.register_presigned_upload(
            db, tenant_id=api_key.tenant_id, payload=payload
        )
        await db.commit()
        return {
            "asset_id": str(asset.id),
            "upload_url": url,
            "method": "PUT",
            "headers": headers,
            "storage_key": asset.storage_key,
            "expires_in_seconds": 900,
        }


# ============================================================
# 5. confirm_upload — flip status to ready + start processing
# ============================================================
@mcp.tool()
async def confirm_upload(asset_id: str) -> dict[str, Any]:
    """Verify object exists in storage and start the processing pipeline."""
    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        asset = await asset_service.confirm_upload(
            db, tenant_id=api_key.tenant_id, asset_id=uuid.UUID(asset_id)
        )
        await db.commit()
        return _summary(asset)


# ============================================================
# 6. multipart_init — multipart upload for large files
# ============================================================
@mcp.tool()
async def multipart_init(
    project_id: str,
    filename: str,
    mime_type: str,
    size_bytes: int,
    sha256: str | None = None,
    acl: str = "project",
) -> dict[str, Any]:
    """Initiate a multipart upload. Returns upload_id. Recommend 8MiB parts."""
    from app.schemas.upload import MultipartInitIn
    from app.services import upload_service

    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        proj_uuid = uuid.UUID(project_id)
        if api_key.project_id and api_key.project_id != proj_uuid:
            raise PermissionError("API key scoped to a different project")
        asset, mp = await upload_service.init_multipart(
            db,
            tenant_id=api_key.tenant_id,
            payload=MultipartInitIn(
                project_id=proj_uuid,
                filename=filename,
                mime_type=mime_type,
                size_bytes=size_bytes,
                sha256=sha256,
                acl=acl,
            ),
        )
        await db.commit()
        return {
            "asset_id": str(asset.id),
            "upload_id": mp.upload_id,
            "storage_key": asset.storage_key,
            "part_size_bytes": 8 * 1024 * 1024,
        }


# ============================================================
# 7. multipart_sign_part
# ============================================================
@mcp.tool()
async def multipart_sign_part(asset_id: str, part_number: int) -> dict[str, Any]:
    """Get a presigned PUT URL for a single part (1..10000)."""
    from app.services import upload_service

    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        url = await upload_service.sign_part(
            db,
            tenant_id=api_key.tenant_id,
            asset_id=uuid.UUID(asset_id),
            part_number=part_number,
        )
        return {"upload_url": url, "expires_in": 3600}


# ============================================================
# 8. multipart_complete
# ============================================================
@mcp.tool()
async def multipart_complete(asset_id: str, parts: list[dict]) -> dict[str, Any]:
    """Finalize: parts = [{'part_number': int, 'etag': str}, ...]"""
    from app.services import upload_service

    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        s3_parts = [
            {"PartNumber": p["part_number"], "ETag": p["etag"]}
            for p in sorted(parts, key=lambda x: x["part_number"])
        ]
        asset = await upload_service.complete(
            db,
            tenant_id=api_key.tenant_id,
            asset_id=uuid.UUID(asset_id),
            parts=s3_parts,
        )
        await db.commit()
        try:
            from app.workers.tasks_pipeline import process_pipeline
            process_pipeline.delay(str(asset.id))
        except Exception:  # noqa: BLE001
            pass
        return _summary(asset)


# ============================================================
# 9. list_projects
# ============================================================
@mcp.tool()
async def list_projects() -> list[dict[str, Any]]:
    """List all projects in the API key's tenant."""
    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        rows = (
            await db.execute(
                select(Project).where(
                    Project.tenant_id == api_key.tenant_id,
                    Project.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        if api_key.project_id:
            rows = [r for r in rows if r.id == api_key.project_id]
        return [
            {
                "id": str(r.id),
                "slug": r.slug,
                "name": r.name,
                "default_acl": r.default_acl,
            }
            for r in rows
        ]


# ============================================================
# 10. update_asset_tags — add / remove manual tags
# ============================================================
@mcp.tool()
async def update_asset_tags(
    asset_id: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> dict[str, Any]:
    """Add and/or remove manual tags on an asset."""
    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        asset = await asset_service.get_asset(
            db, tenant_id=api_key.tenant_id, asset_id=uuid.UUID(asset_id)
        )
        tags = set(asset.manual_tags or [])
        if remove:
            tags -= set(remove)
        if add:
            tags |= set(add)
        asset.manual_tags = sorted(tags)
        await db.flush()
        await db.commit()
        return {"asset_id": asset_id, "manual_tags": asset.manual_tags}


# ============================================================
# 11. get_download_url — presigned GET URL for direct fetch
# ============================================================
@mcp.tool()
async def get_download_url(asset_id: str, expires_in: int = 3600) -> dict[str, Any]:
    """Get a short-lived signed URL the caller can fetch directly."""
    from app.services import storage

    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        asset = await asset_service.get_asset(
            db, tenant_id=api_key.tenant_id, asset_id=uuid.UUID(asset_id)
        )
        url = storage.presign_get(storage_key=asset.storage_key, expires_in=expires_in)
        return {"url": url, "expires_in": expires_in}


# ============================================================
# 12. delete_asset — soft delete
# ============================================================
@mcp.tool()
async def delete_asset(asset_id: str) -> dict[str, Any]:
    """Soft-delete: marks deleted_at + status=archived. Bytes stay in storage."""
    async with AsyncSessionLocal() as db:
        api_key = await _resolve_principal(db)
        await asset_service.soft_delete_asset(
            db, tenant_id=api_key.tenant_id, asset_id=uuid.UUID(asset_id)
        )
        await db.commit()
        return {"asset_id": asset_id, "status": "deleted"}


# ----- formatters -----

def _summary(a) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "name": a.name,
        "kind": a.kind,
        "mime_type": a.mime_type,
        "size_bytes": a.size_bytes,
        "status": a.status,
        "acl": a.acl,
        "tags": (a.manual_tags or []) + (a.auto_tags or []),
        "public_url": a.public_url,
        "created_at": a.created_at.isoformat(),
    }


def _full(a) -> dict[str, Any]:
    return {
        **_summary(a),
        "tenant_id": str(a.tenant_id),
        "project_id": str(a.project_id),
        "description": a.description,
        "sha256": a.sha256,
        "extension": a.extension,
        "storage_key": a.storage_key,
        "width": a.width,
        "height": a.height,
        "duration_seconds": a.duration_seconds,
        "page_count": a.page_count,
        "thumbnails": a.thumbnails,
        "ai_summary": a.ai_summary,
        "ai_alt_text": a.ai_alt_text,
        "ai_visual_description": a.ai_visual_description,
        "current_version": a.current_version,
        "is_starred": a.is_starred,
        "custom_fields": a.custom_fields,
        "updated_at": a.updated_at.isoformat(),
    }


# ----- entrypoint -----

def main() -> None:
    parser = argparse.ArgumentParser(description="QideDAM MCP Server")
    parser.add_argument("--api-key", help="API key (overrides DAM_API_KEY env)")
    parser.add_argument("--transport", default="stdio", choices=["stdio"])
    args = parser.parse_args()
    if args.api_key:
        os.environ["DAM_API_KEY"] = args.api_key
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
