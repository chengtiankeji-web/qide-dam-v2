"""S2 · 诊断 PDF 渲染 · 上传 DAM · 写回 signed URL · 触发 dam.workspace_ready 后续邮件

跑顺序：
  1. process_diagnostic_ready (in tasks_qm_pipeline.py) → 触发 qm.render_diagnostic_pdf
  2. qm.render_diagnostic_pdf：
     a. render_diagnostic_pdf_bytes() 用 reportlab 生成
     b. 上传到 DAM workspace · /20-诊断报告/01-PDF/<onboarding_id>.pdf
     c. 生成 24h signed URL · 回写 diagnostic.pdf_signed_url + pdf_signed_until + pdf_asset_id
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.logging import get_logger
from app.workers.celery_app import celery_app

logger = get_logger("tasks_qm_diagnostic")


@celery_app.task(name="qm.render_diagnostic_pdf", bind=True, queue="media", max_retries=3)
def render_diagnostic_pdf_task(self, diagnostic_id: str) -> dict:
    """渲染诊断 PDF · 上传 DAM · 写回 signed URL"""
    try:
        return asyncio.run(_render_pdf_async(diagnostic_id))
    except Exception as exc:
        logger.error("qm.pdf.render_failed", diagnostic_id=diagnostic_id, error=str(exc)[:300])
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


async def _render_pdf_async(diagnostic_id: str) -> dict:
    from app.db.session import get_session_factory
    from app.models.qidematrix.pipeline import QmDiagnostic, QmOnboarding
    from app.services.qidematrix import diagnostic_service, onboarding_service
    from app.services.qidematrix.diagnostic_service import (
        render_diagnostic_pdf_bytes,
        set_pdf_url,
    )

    diag_id = uuid.UUID(diagnostic_id)
    session_factory = get_session_factory()

    async with session_factory() as db:
        diag = await diagnostic_service.get_diagnostic(db, diagnostic_id=diag_id)
        if not diag:
            return {"ok": False, "error": "diagnostic not found"}
        if diag.status != "ready":
            return {"ok": False, "error": f"diagnostic status={diag.status}"}
        if diag.pdf_signed_url and diag.pdf_signed_until and diag.pdf_signed_until > datetime.now(UTC):
            return {"ok": True, "noop": "pdf already exists"}

        ob = await onboarding_service.get_onboarding(db, onboarding_id=diag.onboarding_id)
        if not ob:
            return {"ok": False, "error": "onboarding not found"}

        # 1. 渲染 PDF bytes
        pdf_bytes = render_diagnostic_pdf_bytes(diag, ob)
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        # 2. 上传 DAM
        from app.services import asset_service, storage, upload_service

        if not ob.workspace_id:
            # workspace 还没建 · 推迟 PDF（dam.workspace_ready 后会重触发）
            logger.info("qm.pdf.workspace_pending", diagnostic_id=str(diag.id))
            return {"ok": True, "deferred": "no workspace yet"}

        from app.models.project import Project
        from app.models.folder import Folder

        # 找客户 project
        project_q = await db.execute(
            select(Project).where(
                Project.tenant_id == ob.tenant_id,
                Project.slug.like("qm-%"),
            ).order_by(Project.created_at.desc())
        )
        # 简化：找最新一个匹配的（生产应该按 workspace_id 关联）
        project = None
        for p in project_q.scalars():
            if p.name == ob.factory_name:
                project = p
                break
        if not project:
            return {"ok": False, "error": "project not found for workspace"}

        # 找 /20-诊断报告/01-PDF folder
        folder_q = await db.execute(
            select(Folder).where(
                Folder.project_id == project.id,
                Folder.path == "/20-诊断报告/01-PDF",
            )
        )
        pdf_folder = folder_q.scalar_one_or_none()
        folder_id = pdf_folder.id if pdf_folder else None

        # 上传到 R2
        filename = f"diagnostic-{ob.factory_name}-{diag.id.hex[:8]}.pdf"
        storage_key = (
            f"qm/{ob.tenant_id}/{project.id}/diagnostics/"
            f"{diag.id.hex}.pdf"
        )

        try:
            storage.put_object(
                storage_key=storage_key,
                body=pdf_bytes,
                content_type="application/pdf",
            )
        except Exception as exc:
            logger.error("qm.pdf.r2_upload_failed", error=str(exc)[:300])
            return {"ok": False, "error": f"r2 upload: {exc}"}

        # 写 Asset 行
        from app.core.config import settings as _settings
        from app.models.asset import Asset
        now = datetime.now(UTC)
        asset = Asset(
            id=uuid.uuid4(),
            tenant_id=ob.tenant_id,
            project_id=project.id,
            folder_id=folder_id,
            name=filename,
            kind="document",
            extension="pdf",
            mime_type="application/pdf",
            sha256=pdf_sha,
            size_bytes=len(pdf_bytes),
            storage_key=storage_key,
            storage_bucket=_settings.S3_BUCKET,  # 必填 · nullable=False
            sensitivity_level="internal",
            status="ready",
            manual_tags=["diagnostic", "auto-generated", "qidematrix"],
            created_at=now,
            updated_at=now,
        )
        db.add(asset)
        await db.flush()

        # 3. 生成 signed URL (24h)
        signed_url = storage.presign_get(
            storage_key=storage_key,
            expires_in=86400,
        )

        diag.pdf_asset_id = asset.id
        set_pdf_url(diag, signed_url=signed_url, ttl_hours=24)
        await db.flush()
        await db.commit()

        logger.info(
            "qm.pdf.uploaded",
            diagnostic_id=str(diag.id),
            asset_id=str(asset.id),
            size_kb=round(len(pdf_bytes) / 1024, 1),
        )

        return {
            "ok": True,
            "diagnostic_id": str(diag.id),
            "asset_id": str(asset.id),
            "size_bytes": len(pdf_bytes),
        }
