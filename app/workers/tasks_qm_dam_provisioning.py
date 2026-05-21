"""S3 · DAM workspace 自动 provisioning Celery task

只是一个事件 dispatcher 的入口 wrapper · 真正逻辑在
dam_provisioning_service.provision_workspace_for_onboarding。

这个 task 不会直接被 pipeline_drain 调（因为 dam.workspace_ready 是
provisioning service 完成后自己 publish 的 · 不是触发它的事件）。

留这个 task 是给手动重试用：
  celery_app.send_task("qm.provision_workspace", args=[onboarding_id])
"""
from __future__ import annotations

import asyncio
import uuid

from app.core.logging import get_logger
from app.workers.celery_app import celery_app

logger = get_logger("tasks_qm_dam_provisioning")


@celery_app.task(name="qm.provision_workspace", bind=True, queue="default")
def provision_workspace_task(self, onboarding_id: str) -> dict:
    """手动重试 workspace provisioning"""
    return asyncio.run(_provision_async(onboarding_id))


async def _provision_async(onboarding_id: str) -> dict:
    from app.db.session import get_session_factory
    from app.services.qidematrix import dam_provisioning_service, onboarding_service
    from app.services.qidematrix.workspace_service import _get_or_create_customer_user

    ob_id = uuid.UUID(onboarding_id)
    session_factory = get_session_factory()

    async with session_factory() as db:
        ob = await onboarding_service.get_onboarding(db, onboarding_id=ob_id)
        if not ob:
            return {"ok": False, "error": "onboarding not found"}

        if ob.workspace_id:
            return {"ok": True, "noop": "workspace already exists",
                    "workspace_id": str(ob.workspace_id)}

        owner_id = await _get_or_create_customer_user(
            db, tenant_id=ob.tenant_id,
            email=ob.contact_email, name=ob.contact_name,
        )
        ws = await dam_provisioning_service.provision_workspace_for_onboarding(
            db, onboarding=ob, owner_user_id=owner_id,
        )
        await db.commit()

        logger.info(
            "qm.workspace.provisioned",
            workspace_id=str(ws.id),
            onboarding_id=str(ob_id),
        )
        return {"ok": True, "workspace_id": str(ws.id), "slug": ws.slug}
