"""Migrate 乡约顺德 67 merchant assets from WeChat cloud storage → QideDAM.

Reads a manifest CSV/TSV (or wechat-cloudbase tcb listing) and registers each
file as an Asset under the `hemei` tenant + `xiangyue-shunde` project.

Manifest format (one per line):
    <fileID> <local_filename> <merchant_name>

Usage:
    python -m scripts.migrate_xiangyue \\
        --manifest /path/to/merchants_assets_manifest.tsv \\
        --tenant-slug hemei --project-slug xiangyue-shunde \\
        --download-dir /tmp/xiangyue_dl

The script:
  1. Looks up tenant + project by slug
  2. For each row: download from WeChat cloud storage (uses tcb CLI if available)
  3. Registers an Asset (source='migration') and uploads to S3 storage
  4. Tags with merchant name, sets manual_tags=['乡约顺德', merchant_name]
  5. Prints a CSV of (asset_id, original_fileID, storage_key) for traceability
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import mimetypes
import os
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import select

from app.core.logging import configure_logging, get_logger
from app.db.session import AsyncSessionLocal
from app.models.asset import Asset
from app.models.project import Project
from app.models.tenant import Tenant
from app.services import asset_service, storage

configure_logging()
logger = get_logger("migrate_xiangyue")


def _try_download_wechat(file_id: str, env_id: str, dest: Path) -> bool:
    """Attempt to download a cloud:// fileID using the tcb CLI."""
    try:
        subprocess.run(
            ["tcb", "storage", "download", file_id, str(dest), "-e", env_id],
            check=True, capture_output=True, timeout=120,
        )
        return dest.exists() and dest.stat().st_size > 0
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


async def migrate(
    manifest: Path,
    tenant_slug: str,
    project_slug: str,
    download_dir: Path,
    env_id: str,
    out_csv: Path,
) -> None:
    download_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []

    async with AsyncSessionLocal() as db:
        tenant = (
            await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
        ).scalar_one_or_none()
        if not tenant:
            sys.exit(f"tenant '{tenant_slug}' not found")
        project = (
            await db.execute(
                select(Project).where(
                    Project.tenant_id == tenant.id, Project.slug == project_slug
                )
            )
        ).scalar_one_or_none()
        if not project:
            sys.exit(f"project '{project_slug}' not found in tenant '{tenant_slug}'")

        with manifest.open() as fh:
            reader = csv.reader(fh, delimiter="\t")
            for line in reader:
                if not line or line[0].startswith("#"):
                    continue
                if len(line) < 2:
                    continue
                file_id = line[0].strip()
                filename = line[1].strip()
                merchant = line[2].strip() if len(line) > 2 else ""

                local_path = download_dir / filename
                if not local_path.exists():
                    if not _try_download_wechat(file_id, env_id, local_path):
                        logger.warning("migrate.skip.download_failed", file_id=file_id)
                        continue

                size = local_path.stat().st_size
                mime = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
                ext = asset_service.safe_extension(filename, mime)
                kind = asset_service.classify_kind(mime, ext)
                sha = _sha256(local_path)

                asset_id = uuid.uuid4()
                storage_key = storage.build_storage_key(
                    tenant_storage_prefix=tenant.storage_prefix,
                    project_storage_prefix=project.storage_prefix,
                    asset_id=asset_id,
                    extension=ext,
                )
                with local_path.open("rb") as fh2:
                    storage.put_object(
                        storage_key=storage_key, body=fh2.read(), content_type=mime
                    )

                tags = ["乡约顺德"]
                if merchant:
                    tags.append(merchant)

                asset = Asset(
                    id=asset_id,
                    tenant_id=tenant.id,
                    project_id=project.id,
                    name=filename,
                    sha256=sha,
                    kind=kind,
                    mime_type=mime,
                    extension=ext,
                    size_bytes=size,
                    storage_key=storage_key,
                    storage_bucket=os.getenv("S3_BUCKET", "qidedam-dev"),
                    status="ready",
                    source="migration",
                    acl="tenant",
                    manual_tags=tags,
                    custom_fields={
                        "wechat_file_id": file_id,
                        "merchant": merchant,
                    },
                )
                db.add(asset)
                rows.append({
                    "asset_id": str(asset_id),
                    "wechat_file_id": file_id,
                    "filename": filename,
                    "merchant": merchant,
                    "storage_key": storage_key,
                })
                if len(rows) % 10 == 0:
                    await db.flush()
                    logger.info("migrate.progress", migrated=len(rows))
        await db.commit()

    with out_csv.open("w", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["asset_id", "wechat_file_id", "filename", "merchant", "storage_key"]
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"Done. Migrated {len(rows)} assets. Manifest written to {out_csv}.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--tenant-slug", default="hemei")
    parser.add_argument("--project-slug", default="xiangyue-shunde")
    parser.add_argument("--download-dir", default="/tmp/xiangyue_dl", type=Path)
    parser.add_argument("--env-id", default="cloud1-d3g818fgt7833accf",
                        help="WeChat cloudbase env ID")
    parser.add_argument("--out", default="xiangyue_migration_result.csv", type=Path)
    args = parser.parse_args()
    asyncio.run(
        migrate(
            args.manifest, args.tenant_slug, args.project_slug,
            args.download_dir, args.env_id, args.out,
        )
    )


if __name__ == "__main__":
    main()
