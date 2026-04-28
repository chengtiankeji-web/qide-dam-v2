from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel


class UsageDayOut(BaseModel):
    tenant_id: uuid.UUID
    day: date
    storage_bytes_total: int
    upload_bytes: int
    download_bytes: int
    asset_count_total: int
    new_asset_count: int
    ai_calls: int
    ai_input_tokens: int
    ai_output_tokens: int
    webhook_deliveries: int

    model_config = {"from_attributes": True}


class UsageSummaryOut(BaseModel):
    tenant_id: uuid.UUID
    period_from: date
    period_to: date
    days: list[UsageDayOut]
    total_storage_bytes: int
    total_upload_bytes: int
    total_download_bytes: int
    total_ai_calls: int
    total_webhook_deliveries: int
