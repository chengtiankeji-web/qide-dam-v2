"""Video processing — duration + first-frame thumbnail via ffmpeg.

Falls back gracefully if ffmpeg isn't installed: marks asset ready without
thumbnail and logs the omission. Production Docker image bundles ffmpeg.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from app.core.logging import get_logger
from app.models.asset import Asset
from app.services import storage
from app.workers._db import session_scope
from app.workers.celery_app import celery_app

logger = get_logger("worker.video")


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _probe_duration(path: Path) -> float | None:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return float(json.loads(proc.stdout)["format"]["duration"])
    except Exception:  # noqa: BLE001
        return None


def _grab_first_frame(path: Path, out_path: Path) -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-vframes", "1", "-q:v", "3", str(out_path)],
            check=True, capture_output=True, timeout=60,
        )
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception:  # noqa: BLE001
        return False


@celery_app.task(name="video.process", bind=True, max_retries=2, default_retry_delay=60)
def process_video(self, asset_id: str) -> dict:
    logger.info("video.process.start", asset_id=asset_id)
    try:
        with session_scope() as db:
            asset = db.get(Asset, uuid.UUID(asset_id))
            if not asset:
                return {"asset_id": asset_id, "status": "missing"}

            if not _has_ffmpeg():
                logger.warning("video.process.no_ffmpeg", asset_id=asset_id)
                return {"asset_id": asset_id, "status": "skipped_no_ffmpeg"}

            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / f"src.{asset.extension}"
                src.write_bytes(storage.get_object(asset.storage_key))

                duration = _probe_duration(src)
                if duration is not None:
                    asset.duration_seconds = duration

                frame = Path(tmp) / "frame.jpg"
                if _grab_first_frame(src, frame):
                    # Generate the same 3-tier thumbnails using the frame
                    from PIL import Image

                    from app.workers.tasks_image import THUMB_SIZES, _make_thumbnail
                    im = Image.open(frame)
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

            db.add(asset)
        logger.info("video.process.done", asset_id=asset_id)
        return {"asset_id": asset_id, "status": "ok"}
    except Exception as exc:  # noqa: BLE001
        logger.error("video.process.error", asset_id=asset_id, error=str(exc))
        raise self.retry(exc=exc)
