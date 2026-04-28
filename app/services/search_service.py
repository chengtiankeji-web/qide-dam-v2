"""Vector + hybrid search backed by pgvector cosine similarity.

`embedding` column is `vector(768)` (alembic 001 + IVF cosine index).
Cosine distance in pgvector: `<=>` operator returns a value in [0, 2].
We convert to a similarity score in [0, 1] via `1 - distance/2`.
"""
from __future__ import annotations

import uuid

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.services import ai_service


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


async def search_by_vector(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    embedding: list[float],
    project_id: uuid.UUID | None = None,
    kind: str | None = None,
    limit: int = 20,
    min_similarity: float = 0.0,
) -> list[tuple[Asset, float]]:
    """Returns [(asset, similarity), ...] sorted by similarity desc."""
    sql = text(
        """
        SELECT a.*, (a.embedding <=> CAST(:v AS vector)) AS distance
        FROM assets a
        WHERE a.tenant_id = :tenant_id
          AND a.deleted_at IS NULL
          AND a.embedding IS NOT NULL
          AND (:project_id IS NULL OR a.project_id = :project_id)
          AND (:kind IS NULL OR a.kind = :kind)
        ORDER BY a.embedding <=> CAST(:v AS vector)
        LIMIT :lim
        """
    ).bindparams(
        bindparam("v", value=_vec_literal(embedding)),
        bindparam("tenant_id", value=str(tenant_id)),
        bindparam("project_id", value=str(project_id) if project_id else None),
        bindparam("kind", value=kind),
        bindparam("lim", value=limit),
    )
    res = await db.execute(sql)
    out: list[tuple[Asset, float]] = []
    for row in res.mappings():
        distance = float(row["distance"])
        similarity = max(0.0, 1.0 - distance / 2.0)
        if similarity < min_similarity:
            continue
        # Reconstitute Asset from row mapping
        cols = {c.name: row[c.name] for c in Asset.__table__.columns if c.name in row}
        asset = Asset(**cols)
        out.append((asset, similarity))
    return out


async def search_by_text(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    text_query: str,
    project_id: uuid.UUID | None = None,
    kind: str | None = None,
    limit: int = 20,
    min_similarity: float = 0.0,
) -> list[tuple[Asset, float]]:
    vec = ai_service.embed_text(text_query)
    return await search_by_vector(
        db,
        tenant_id=tenant_id,
        embedding=vec,
        project_id=project_id,
        kind=kind,
        limit=limit,
        min_similarity=min_similarity,
    )


async def search_similar_to_asset(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    asset_id: uuid.UUID,
    limit: int = 20,
    min_similarity: float = 0.0,
) -> list[tuple[Asset, float]]:
    """Find assets with similar embedding to the given asset."""
    src_emb = (
        await db.execute(
            text("SELECT embedding::text AS e FROM assets WHERE id = :id AND tenant_id = :t"),
            {"id": str(asset_id), "t": str(tenant_id)},
        )
    ).first()
    if not src_emb or not src_emb[0]:
        return []
    raw = src_emb[0]
    if raw.startswith("[") and raw.endswith("]"):
        vec = [float(x) for x in raw[1:-1].split(",")]
    else:
        return []
    results = await search_by_vector(
        db, tenant_id=tenant_id, embedding=vec, limit=limit + 1,
        min_similarity=min_similarity,
    )
    # Drop self
    return [(a, s) for a, s in results if a.id != asset_id][:limit]
