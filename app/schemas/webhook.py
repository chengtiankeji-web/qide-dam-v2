from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl


class WebhookSubscriptionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    target_url: HttpUrl
    events: list[str] = Field(default_factory=list)
    project_id: uuid.UUID | None = None


class WebhookSubscriptionOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID | None
    name: str
    target_url: str
    events: list[str]
    is_active: bool
    consecutive_failures: int
    suspended_at: datetime | None
    last_delivered_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class WebhookSubscriptionCreateOut(WebhookSubscriptionOut):
    secret: str  # full secret returned only at create time


class WebhookDeliveryOut(BaseModel):
    id: uuid.UUID
    subscription_id: uuid.UUID
    event_type: str
    status: str
    attempt_count: int
    response_status: int | None
    error: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
