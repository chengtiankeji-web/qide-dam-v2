from __future__ import annotations

import uuid

from pydantic import BaseModel, EmailStr, Field


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    tenant_slug: str | None = None  # if user belongs to multiple tenants


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    project_id: uuid.UUID | None = None
    scopes: list[str] = Field(default_factory=list)


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    project_id: uuid.UUID | None
    is_active: bool
    expires_at: str | None = None
    created_at: str

    model_config = {"from_attributes": True}


class ApiKeyCreateOut(ApiKeyOut):
    raw_key: str  # shown only once
