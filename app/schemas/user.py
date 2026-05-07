"""User schemas — list / create / update / change-password / soft-delete."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

VALID_ROLES = {"platform_admin", "tenant_admin", "member", "viewer"}


class UserOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    full_name: str
    role: str
    is_active: bool
    is_platform_admin: bool
    project_access: list  # list of project UUIDs as str, or ["*"] meaning all
    created_at: datetime

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    """Invite a new user (admin-only).

    tenant_id is set from the current admin's tenant (or supplied for platform_admin).
    """
    email: EmailStr
    full_name: str = Field(default="", max_length=128)
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(default="member", pattern=r"^(platform_admin|tenant_admin|member|viewer)$")
    tenant_id: uuid.UUID | None = None  # platform_admin only · cross-tenant invite
    project_access: list[str] = Field(default_factory=list)


class UserUpdate(BaseModel):
    """Patch a user · admin-only · email + tenant immutable."""
    full_name: str | None = Field(default=None, max_length=128)
    role: str | None = Field(default=None, pattern=r"^(platform_admin|tenant_admin|member|viewer)$")
    is_active: bool | None = None
    project_access: list[str] | None = None


class PasswordChange(BaseModel):
    """Self change password · current_password verified."""
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class PasswordReset(BaseModel):
    """Admin reset another user's password · no current_password required."""
    new_password: str = Field(min_length=8, max_length=128)
