"""AI workers — auto-tag images + generate embeddings.

Both tasks are idempotent: re-running on the same asset overwrites previous
AI fields without creating duplicates.
"""
from __future__ import annotations

import datetime
import uuid

from sqlalchemy import text

from app.core.logging import get_logger
from app.models.asset import Asset
from app.services import ai_service, storage
from app.workers._db import session_scope
from app.workers.celery_app import celery_app

logger = get_logger("worker.ai")


def _load_thumb_or_original(asset: Asset) -> bytes:
    """Prefer the medium thumbnail (cheap to embed). Fall back to original."""
    md_key = (asset.thumbnails or {}).get("md")
    if md_key:
        try:
            return storage.get_object(md_key)
        except Exception:  # noqa: BLE001
            pass
    return storage.get_object(asset.storage_key)


@celery_app.task(name="ai.tag", bind=True, max_retries=2, default_retry_delay=60)
def auto_tag(self, asset_id: str) -> dict:
    logger.info("ai.tag.start", asset_id=asset_id)
    try:
        with session_scope() as db:
            asset = db.get(Asset, uuid.UUID(asset_id))
            if not asset or asset.kind != "image":
                return {"asset_id": asset_id, "status": "skipped"}

            image_bytes = _load_thumb_or_original(asset)
            result = ai_service.tag_image(image_bytes)

            tags = [str(t)[:64] for t in result.get("tags", []) if t]
            asset.auto_tags = tags
            asset.ai_summary = (result.get("summary") or None)
            asset.ai_alt_text = (result.get("alt_text") or None)
            asset.ai_visual_description = (result.get("visual_description") or None)
            asset.ai_model = "qwen-vl-plus" if ai_service.has_provider() else "stub"
            asset.ai_processed_at = datetime.datetime.now(datetime.UTC)
            db.add(asset)
        logger.info("ai.tag.done", asset_id=asset_id, tag_count=len(tags))
        return {"asset_id": asset_id, "status": "ok", "tags": tags}
    except Exception as exc:  # noqa: BLE001
        logger.error("ai.tag.error", asset_id=asset_id, error=str(exc))
        raise self.retry(exc=exc)


@celery_app.task(name="ai.embed", bind=True, max_retries=2, default_retry_delay=60)
def embed_asset(self, asset_id: str) -> dict:
    logger.info("ai.embed.start", asset_id=asset_id)
    try:
        with session_scope() as db:
            asset = db.get(Asset, uuid.UUID(asset_id))
            if not asset:
                return {"asset_id": asset_id, "status": "missing"}

            # Embedding strategy by kind:
            # - image: VL caption → text-embedding
            # - video / document / audio / other: combine name + tags + description
            if asset.kind == "image":
                try:
                    image_bytes = _load_thumb_or_original(asset)
                    hint = " ".join(asset.manual_tags or []) or asset.name
                    vec = ai_service.embed_image(image_bytes, hint_text=hint)
                except Exception as e:  # noqa: BLE001
                    logger.warning("ai.embed.image_failed", error=str(e))
                    vec = ai_service.embed_text(asset.name)
            else:
                parts = [asset.name, asset.description or ""]
                parts += list(asset.manual_tags or []) + list(asset.auto_tags or [])
                if asset.ai_visual_description:
                    parts.append(asset.ai_visual_description)
                if asset.ai_summary:
                    parts.append(asset.ai_summary)
                vec = ai_service.embed_text(" ".join(p for p in parts if p))

            # Use raw SQL for the vector column since SQLA core doesn't carry the type
            db.execute(
                text("UPDATE assets SET embedding = :v WHERE id = :id"),
                {
                    "v": "[" + ",".join(f"{x:.6f}" for x in vec) + "]",
                    "id": str(asset.id),
                },
            )
        logger.info("ai.embed.done", asset_id=asset_id)
        return {"asset_id": asset_id, "status": "ok", "dim": len(vec)}
    except Exception as exc:  # noqa: BLE001
        logger.error("ai.embed.error", asset_id=asset_id, error=str(exc))
        raise self.retry(exc=exc)
