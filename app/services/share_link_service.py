from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, verify_password
from app.models.share_link import ShareLink


def _generate_token() -> str:
    return secrets.token_urlsafe(24)


async def create_link(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_id: uuid.UUID | None,
    collection_id: uuid.UUID | None,
    created_by_user_id: uuid.UUID | None,
    password: str | None,
    expires_at: datetime | None,
    max_downloads: int | None,
    note: str | None,
) -> ShareLink:
    if not asset_id and not collection_id:
        raise ValueError("must specify asset_id or collection_id")
    sl = ShareLink(
        tenant_id=tenant_id,
        asset_id=asset_id,
        collection_id=collection_id,
        created_by_user_id=created_by_user_id,
        token=_generate_token(),
        password_hash=hash_password(password) if password else None,
        expires_at=expires_at,
        max_downloads=max_downloads,
        note=note,
    )
    db.add(sl)
    await db.flush()
    return sl


async def resolve_link(
    db: AsyncSession, *, token: str, password: str | None
) -> ShareLink:
    sl = (
        await db.execute(select(ShareLink).where(ShareLink.token == token))
    ).scalar_one_or_none()
    if not sl or not sl.is_active:
        raise ValueError("invalid or revoked link")
    if sl.expires_at and sl.expires_at < datetime.now(timezone.utc):
        raise ValueError("link expired")
    if sl.max_downloads is not None and sl.download_count >= sl.max_downloads:
        raise ValueError("download quota exhausted")
    if sl.password_hash:
        if not password or not verify_password(password, sl.password_hash):
            raise ValueError("password required or incorrect")
    sl.download_count += 1
    await db.flush()
    return sl
