"""Image processing — generate thumbnails, extract EXIF / dims.

Thumbnails: sm 256, md 768, lg 1600 — JPEG q=82.
"""
from __future__ import annotations

import io
import uuid
from typing import Any

from PIL import ExifTags, Image

from app.core.logging import get_logger
from app.models.asset import Asset
from app.services import storage
from app.workers._db import session_scope
from app.workers.celery_app import celery_app

logger = get_logger("worker.image")

THUMB_SIZES = {"sm": 256, "md": 768, "lg": 1600}


def _make_thumbnail(im: Image.Image, max_side: int) -> bytes:
    work = im.copy()
    if work.mode not in ("RGB", "RGBA"):
        work = work.convert("RGB")
    elif work.mode == "RGBA":
        # Flatten transparent areas onto white so JPEG output looks right
        bg = Image.new("RGB", work.size, (255, 255, 255))
        bg.paste(work, mask=work.split()[-1])
        work = bg
    work.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    work.save(buf, format="JPEG", quality=82, optimize=True)
    return buf.getvalue()


def _extract_exif(im: Image.Image) -> dict[str, Any]:
    raw = getattr(im, "_getexif", lambda: None)()
    if not raw:
        return {}
    out: dict[str, Any] = {}
    for tag, val in raw.items():
        name = ExifTags.TAGS.get(tag, str(tag))
        if isinstance(val, bytes):
            try:
                val = val.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                continue
        try:
            # Filter out non-JSON-serializable types
            import json
            json.dumps({name: val}, default=str)
            out[name] = val if not isinstance(val, (bytes, bytearray)) else str(val)
        except Exception:  # noqa: BLE001
            continue
    return out


@celery_app.task(name="image.process", bind=True, max_retries=3, default_retry_delay=30)
def process_image(self, asset_id: str) -> dict:
    logger.info("image.process.start", asset_id=asset_id)
    try:
        with session_scope() as db:
            asset = db.get(Asset, uuid.UUID(asset_id))
            if not asset:
                return {"asset_id": asset_id, "status": "missing"}

            body = storage.get_object(asset.storage_key)
            im = Image.open(io.BytesIO(body))
            asset.width, asset.height = im.size

            thumbs: dict[str, str] = {}
            for size_name, max_side in THUMB_SIZES.items():
                thumb_bytes = _make_thumbnail(im, max_side)
                thumb_key = storage.build_thumbnail_key(asset.storage_key, size_name)
                storage.put_object(
                    storage_key=thumb_key, body=thumb_bytes, content_type="image/jpeg"
                )
                thumbs[size_name] = thumb_key
            asset.thumbnails = thumbs

            exif = _extract_exif(im)
            tech = dict(asset.technical_metadata or {})
            tech["format"] = im.format
            tech["mode"] = im.mode
            if exif:
                tech["exif"] = exif
            asset.technical_metadata = tech
            db.add(asset)
        logger.info("image.process.done", asset_id=asset_id, thumbs=list(thumbs.keys()))
        return {"asset_id": asset_id, "status": "ok", "thumbs": list(thumbs.keys())}
    except Exception as exc:  # noqa: BLE001
        # 2026-04-29 fix: swallow — finalize must always run
        logger.error("image.process.error_swallowed", asset_id=asset_id, error=str(exc))
        return {"asset_id": asset_id, "status": "error", "error": str(exc)[:200]}
