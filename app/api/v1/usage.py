"""Usage — daily counters + per-period summary for billing/admin."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.schemas.usage import UsageDayOut, UsageSummaryOut
from app.services import usage_service

router = APIRouter()


@router.get("/summary", response_model=UsageSummaryOut)
async def get_summary(
    period_from: date | None = Query(None),
    period_to: date | None = Query(None),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> UsageSummaryOut:
    today = date.today()
    period_from = period_from or (today - timedelta(days=30))
    period_to = period_to or today
    rows = await usage_service.summary(
        db, tenant_id=p.tenant_id, period_from=period_from, period_to=period_to
    )
    days = [UsageDayOut.model_validate(r) for r in rows]
    return UsageSummaryOut(
        tenant_id=p.tenant_id,
        period_from=period_from,
        period_to=period_to,
        days=days,
        total_storage_bytes=max((d.storage_bytes_total for d in days), default=0),
        total_upload_bytes=sum(d.upload_bytes for d in days),
        total_download_bytes=sum(d.download_bytes for d in days),
        total_ai_calls=sum(d.ai_calls for d in days),
        total_webhook_deliveries=sum(d.webhook_deliveries for d in days),
    )
