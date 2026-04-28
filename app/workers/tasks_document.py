"""Document processing — PDF page count + first-page cover thumbnail.

Uses pypdf for page count (always present). For cover rendering we try
pdf2image if poppler is available; otherwise we skip the thumbnail.
"""
from __future__ import annotations

import io
import uuid

from app.core.logging import get_logger
from app.models.asset import Asset
from app.services import storage
from app.workers._db import session_scope
from app.workers.celery_app import celery_app

logger = get_logger("worker.document")


def _safe_import_pypdf():
    try:
        from pypdf import PdfReader
        return PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore[no-redef]
            return PdfReader
        except ImportError:
            return None


def _safe_import_pdf2image():
    try:
        import pdf2image
        return pdf2image
    except ImportError:
        return None


@celery_app.task(name="document.process", bind=True, max_retries=2, default_retry_delay=30)
def process_document(self, asset_id: str) -> dict:
    logger.info("document.process.start", asset_id=asset_id)
    try:
        with session_scope() as db:
            asset = db.get(Asset, uuid.UUID(asset_id))
            if not asset:
                return {"asset_id": asset_id, "status": "missing"}

            body = storage.get_object(asset.storage_key)

            # PDF page count (other doc formats: skip silently)
            if asset.extension == "pdf":
                PdfReader = _safe_import_pypdf()
                if PdfReader is not None:
                    try:
                        reader = PdfReader(io.BytesIO(body))
                        asset.page_count = len(reader.pages)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("document.pdf.read_error", error=str(e))

                # Cover thumbnail
                pdf2image = _safe_import_pdf2image()
                if pdf2image is not None:
                    try:
                        pages = pdf2image.convert_from_bytes(
                            body, first_page=1, last_page=1, dpi=110, fmt="jpeg"
                        )
                        if pages:
                            from app.workers.tasks_image import THUMB_SIZES, _make_thumbnail
                            im = pages[0]
                            asset.width, asset.height = im.size
                            thumbs: dict[str, str] = {}
                            for size_name, max_side in THUMB_SIZES.items():
                                bytes_ = _make_thumbnail(im, max_side)
                                key = storage.build_thumbnail_key(asset.storage_key, size_name)
                                storage.put_object(
                                    storage_key=key, body=bytes_, content_type="image/jpeg"
                                )
                                thumbs[size_name] = key
                            asset.thumbnails = thumbs
                    except Exception as e:  # noqa: BLE001
                        logger.warning("document.pdf.render_error", error=str(e))

            db.add(asset)
        logger.info("document.process.done", asset_id=asset_id)
        return {"asset_id": asset_id, "status": "ok"}
    except Exception as exc:  # noqa: BLE001
        logger.error("document.process.error", asset_id=asset_id, error=str(exc))
        raise self.retry(exc=exc)
