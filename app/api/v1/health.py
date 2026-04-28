"""Health endpoints — used by Docker / load balancer / monitoring."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "app": settings.APP_NAME, "env": settings.APP_ENV}


@router.get("/readyz")
async def readyz(db: AsyncSession = Depends(get_db)) -> dict:
    """Verifies DB is reachable. Returns 200 only if the connection works."""
    await db.execute(text("SELECT 1"))
    return {"status": "ready"}
