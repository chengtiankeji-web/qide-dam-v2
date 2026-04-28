"""Vector / semantic search endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.schemas.asset import AssetOut
from app.schemas.search import SearchHit, VectorSearchIn, VectorSearchOut
from app.services import search_service

router = APIRouter()


@router.post("/vector", response_model=VectorSearchOut)
async def vector_search(
    payload: VectorSearchIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> VectorSearchOut:
    if payload.project_id and not p.can_access_project(payload.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to project")

    if payload.asset_id:
        results = await search_service.search_similar_to_asset(
            db,
            tenant_id=p.tenant_id,
            asset_id=payload.asset_id,
            limit=payload.limit,
            min_similarity=payload.min_similarity,
        )
        kind_label = "asset"
    elif payload.text:
        results = await search_service.search_by_text(
            db,
            tenant_id=p.tenant_id,
            text_query=payload.text,
            project_id=payload.project_id,
            kind=payload.kind,
            limit=payload.limit,
            min_similarity=payload.min_similarity,
        )
        kind_label = "text"
    else:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "must supply either asset_id or text"
        )

    return VectorSearchOut(
        query_kind=kind_label,
        items=[
            SearchHit(asset=AssetOut.model_validate(a), similarity=s)
            for a, s in results
        ],
    )
