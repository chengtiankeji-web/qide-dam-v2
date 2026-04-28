from __future__ import annotations

import uuid
from typing import Iterable

from sqlalchemy import and_, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.collection import Collection, CollectionAsset


async def list_collections(
    db: AsyncSession, *, tenant_id: uuid.UUID, project_id: uuid.UUID | None = None
) -> list[Collection]:
    stmt = select(Collection).where(
        Collection.tenant_id == tenant_id, Collection.deleted_at.is_(None)
    )
    if project_id:
        stmt = stmt.where(Collection.project_id == project_id)
    return list((await db.execute(stmt.order_by(Collection.name))).scalars().all())


async def create_collection(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    owner_user_id: uuid.UUID | None,
    slug: str,
    name: str,
    description: str | None,
    project_id: uuid.UUID | None,
    cover_asset_id: uuid.UUID | None,
    acl: str,
    is_smart: bool,
    smart_query: dict,
) -> Collection:
    coll = Collection(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        slug=slug,
        name=name,
        description=description,
        project_id=project_id,
        cover_asset_id=cover_asset_id,
        acl=acl,
        is_smart=is_smart,
        smart_query=smart_query,
    )
    db.add(coll)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise ValueError(f"slug exists: {e}") from e
    return coll


async def add_assets(
    db: AsyncSession,
    *,
    collection_id: uuid.UUID,
    items: Iterable[dict],
) -> int:
    added = 0
    for item in items:
        link = CollectionAsset(
            collection_id=collection_id,
            asset_id=uuid.UUID(str(item["asset_id"])),
            sort_order=int(item.get("sort_order", 0)),
            note=item.get("note"),
        )
        db.add(link)
        try:
            await db.flush()
            added += 1
        except IntegrityError:
            await db.rollback()
            continue
    return added


async def remove_asset(
    db: AsyncSession, *, collection_id: uuid.UUID, asset_id: uuid.UUID
) -> int:
    res = await db.execute(
        delete(CollectionAsset).where(
            and_(
                CollectionAsset.collection_id == collection_id,
                CollectionAsset.asset_id == asset_id,
            )
        )
    )
    return res.rowcount or 0


async def list_assets_in_collection(
    db: AsyncSession,
    *,
    collection_id: uuid.UUID,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[uuid.UUID], int]:
    count_stmt = select(func.count()).select_from(
        select(CollectionAsset.asset_id).where(
            CollectionAsset.collection_id == collection_id
        ).subquery()
    )
    total = (await db.execute(count_stmt)).scalar_one()
    stmt = (
        select(CollectionAsset.asset_id)
        .where(CollectionAsset.collection_id == collection_id)
        .order_by(CollectionAsset.sort_order, CollectionAsset.created_at)
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows), int(total)
