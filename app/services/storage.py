"""Object-storage service — wraps boto3, works for R2 / MinIO / OSS / any S3.

Why a thin wrapper?
- Single entry point for "build storage_key" so prefix conventions stay consistent.
- Single entry point for presigned URL config (region quirks: R2 uses 'auto').
- Single entry point for public URL formatting (CDN domain or signed-URL fallback).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _make_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
        # R2 / MinIO require path-style addressing
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
        use_ssl=settings.S3_USE_SSL,
    )


_client = None


def get_client():
    global _client
    if _client is None:
        _client = _make_client()
    return _client


# ----- key conventions -----

def build_storage_key(
    *,
    tenant_storage_prefix: str,
    project_storage_prefix: str,
    asset_id: uuid.UUID,
    extension: str,
    when: datetime | None = None,
) -> str:
    """Compose: t/<tenant>/p/<project>/<yyyy>/<mm>/<dd>/<asset_id>.<ext>"""
    when = when or datetime.now(UTC)
    ext = extension.lower().lstrip(".")
    return (
        f"t/{tenant_storage_prefix}/p/{project_storage_prefix}/"
        f"{when.year:04d}/{when.month:02d}/{when.day:02d}/{asset_id}.{ext}"
    )


def build_thumbnail_key(asset_storage_key: str, size: str) -> str:
    """thumbnails/<size>/<original-key-without-ext>.jpg"""
    p = PurePosixPath(asset_storage_key)
    stem_dir = p.parent / p.stem
    return f"thumbnails/{size}/{stem_dir}.jpg"


# ----- bucket ops -----

def ensure_bucket() -> None:
    """Create bucket if it does not exist (idempotent — safe in entrypoint)."""
    client = get_client()
    try:
        client.head_bucket(Bucket=settings.S3_BUCKET)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchBucket"):
            logger.info("storage.bucket.create", bucket=settings.S3_BUCKET)
            client.create_bucket(Bucket=settings.S3_BUCKET)
        else:
            raise


# ----- presigned URLs -----

def presign_put(
    *,
    storage_key: str,
    content_type: str,
    expires_in: int = 900,
) -> tuple[str, dict[str, str]]:
    client = get_client()
    url = client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.S3_BUCKET,
            "Key": storage_key,
            "ContentType": content_type,
        },
        ExpiresIn=expires_in,
    )
    return url, {"Content-Type": content_type}


def presign_get(*, storage_key: str, expires_in: int = 3600) -> str:
    client = get_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET, "Key": storage_key},
        ExpiresIn=expires_in,
    )


# ----- direct ops -----

def put_object(*, storage_key: str, body: bytes, content_type: str) -> None:
    get_client().put_object(
        Bucket=settings.S3_BUCKET,
        Key=storage_key,
        Body=body,
        ContentType=content_type,
    )


def head_object(storage_key: str) -> dict | None:
    try:
        return get_client().head_object(Bucket=settings.S3_BUCKET, Key=storage_key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def delete_object(storage_key: str) -> None:
    get_client().delete_object(Bucket=settings.S3_BUCKET, Key=storage_key)


def public_url_for(storage_key: str) -> str | None:
    """Build CDN URL if PUBLIC_BASE_URL is configured. Else None (caller must
    fall back to a presigned GET)."""
    if not settings.S3_PUBLIC_BASE_URL:
        return None
    return f"{settings.S3_PUBLIC_BASE_URL.rstrip('/')}/{storage_key}"


def get_object(storage_key: str) -> bytes:
    """Download object body — used by Celery workers."""
    resp = get_client().get_object(Bucket=settings.S3_BUCKET, Key=storage_key)
    return resp["Body"].read()


# ----- multipart -----

def initiate_multipart(*, storage_key: str, content_type: str) -> str:
    resp = get_client().create_multipart_upload(
        Bucket=settings.S3_BUCKET, Key=storage_key, ContentType=content_type
    )
    return resp["UploadId"]


def presign_upload_part(
    *, storage_key: str, upload_id: str, part_number: int, expires_in: int = 3600
) -> str:
    return get_client().generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": settings.S3_BUCKET,
            "Key": storage_key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        },
        ExpiresIn=expires_in,
    )


def complete_multipart(
    *, storage_key: str, upload_id: str, parts: list[dict]
) -> None:
    """parts: [{'PartNumber': int, 'ETag': str}, ...]"""
    get_client().complete_multipart_upload(
        Bucket=settings.S3_BUCKET,
        Key=storage_key,
        UploadId=upload_id,
        MultipartUpload={"Parts": parts},
    )


def abort_multipart(*, storage_key: str, upload_id: str) -> None:
    get_client().abort_multipart_upload(
        Bucket=settings.S3_BUCKET, Key=storage_key, UploadId=upload_id
    )
