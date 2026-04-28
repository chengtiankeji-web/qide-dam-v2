from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class MultipartInitIn(BaseModel):
    project_id: uuid.UUID
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(ge=1)
    sha256: str | None = None
    acl: str = Field(default="project")
    manual_tags: list[str] = Field(default_factory=list)


class MultipartInitOut(BaseModel):
    asset_id: uuid.UUID
    upload_id: str
    storage_key: str
    part_size_bytes: int = 8 * 1024 * 1024  # 8 MiB recommended


class MultipartSignPartIn(BaseModel):
    part_number: int = Field(ge=1, le=10000)


class MultipartSignPartOut(BaseModel):
    upload_url: str
    headers: dict[str, str]
    expires_in: int


class MultipartCompletePart(BaseModel):
    part_number: int = Field(ge=1, le=10000)
    etag: str = Field(min_length=1, max_length=128)


class MultipartCompleteIn(BaseModel):
    parts: list[MultipartCompletePart] = Field(min_length=1)


class MultipartAbortOut(BaseModel):
    aborted: bool = True
