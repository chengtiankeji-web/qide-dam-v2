"""Usage meters — atomic per-day counters via UPSERT.

Call sites:
- after asset upload confirm:  bump(tenant_id, day, upload_bytes=size, new_asset_count=1)
- presign GET / share download: bump(tenant_id, day, download_bytes=size)
- AI tag/embed tasks: bump(tenant_id, day, ai_calls=1, ai_input_tokens=..., ai_output_tokens=...)
- webhook deliver succeeds: bump(tenant_id, day, webhook_deliveries=1)
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.usage_meter import UsageMeter


async def bump(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    day: date | None = None,
    storage_delta_bytes: int = 0,
    upload_bytes: int = 0,
    download_bytes: int = 0,
    new_asset_count: int = 0,
    ai_calls: int = 0,
    ai_input_tokens: int = 0,
    ai_output_tokens: int = 0,
    webhook_deliveries: int = 0,
) -> None:
    """ON CONFLICT DO UPDATE — single round-trip and crash-safe."""
    day = day or datetime.now(timezone.utc).date()
    sql = text(
        """
        INSERT INTO usage_meters (
            tenant_id, day,
            storage_bytes_total, upload_bytes, download_bytes,
            new_asset_count, ai_calls,
            ai_input_tokens, ai_output_tokens, webhook_deliveries
        )
        VALUES (
            :tenant_id, :day,
            :storage, :upload, :download,
            :new_assets, :ai_calls,
            :ai_in, :ai_out, :webhook
        )
        ON CONFLICT (tenant_id, day) DO UPDATE SET
            storage_bytes_total = usage_meters.storage_bytes_total + EXCLUDED.storage_bytes_total,
            upload_bytes = usage_meters.upload_bytes + EXCLUDED.upload_bytes,
            download_bytes = usage_meters.download_bytes + EXCLUDED.download_bytes,
            new_asset_count = usage_meters.new_asset_count + EXCLUDED.new_asset_count,
            ai_calls = usage_meters.ai_calls + EXCLUDED.ai_calls,
            ai_input_tokens = usage_meters.ai_input_tokens + EXCLUDED.ai_input_tokens,
            ai_output_tokens = usage_meters.ai_output_tokens + EXCLUDED.ai_output_tokens,
            webhook_deliveries = usage_meters.webhook_deliveries + EXCLUDED.webhook_deliveries,
            updated_at = NOW()
        """
    )
    await db.execute(
        sql,
        {
            "tenant_id": str(tenant_id),
            "day": day,
            "storage": storage_delta_bytes,
            "upload": upload_bytes,
            "download": download_bytes,
            "new_assets": new_asset_count,
            "ai_calls": ai_calls,
            "ai_in": ai_input_tokens,
            "ai_out": ai_output_tokens,
            "webhook": webhook_deliveries,
        },
    )


async def summary(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    period_from: date,
    period_to: date,
) -> list[UsageMeter]:
    stmt = select(UsageMeter).where(
        UsageMeter.tenant_id == tenant_id,
        UsageMeter.day >= period_from,
        UsageMeter.day <= period_to,
    ).order_by(UsageMeter.day)
    return list((await db.execute(stmt)).scalars().all())
