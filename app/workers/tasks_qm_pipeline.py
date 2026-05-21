"""QideMatrix v1 · 8 阶段业务流 · 事件总线 dispatcher

设计：
- qm.pipeline_drain · 每 30s 扫一次 pending 事件 · 按 EVENT_HANDLER_MAP 派单到下游 task
- pg_notify 触发的 listener 只是兜底 · drain task 是主路径（重启 / 漏通知 / 错误恢复全靠它）
- 14 个事件类型 → 14 个下游 task 路由
"""
from __future__ import annotations

import asyncio
import uuid

from app.core.logging import get_logger
from app.workers.celery_app import celery_app

logger = get_logger("tasks_qm_pipeline")


# ═════════════════════════════════════════════════════════════════════
# 主入口 · pipeline_drain · beat 每 30s 触发
# ═════════════════════════════════════════════════════════════════════

@celery_app.task(name="qm.pipeline_drain", bind=True, queue="default")
def pipeline_drain_task(self, batch_size: int = 20) -> dict:
    """扫 pending 事件 · 派单"""
    return asyncio.run(_pipeline_drain_async(batch_size))


async def _pipeline_drain_async(batch_size: int) -> dict:
    from app.db.session import get_session_factory
    from app.services.qidematrix import pipeline_service
    from app.services.qidematrix.pipeline_service import EVENT_HANDLER_MAP

    session_factory = get_session_factory()
    dispatched = 0
    failed = 0

    async with session_factory() as db:
        events = await pipeline_service.claim_next_pending(db, limit=batch_size)
        await db.commit()  # 释放行锁 · status 已翻 processing

        for event in events:
            task_name = EVENT_HANDLER_MAP.get(event.event_type)
            if not task_name:
                # 未知事件 → 直接 mark delivered（避免 retry 风暴）
                async with session_factory() as db2:
                    await pipeline_service.mark_delivered(db2, event_id=event.id)
                    await db2.commit()
                logger.warning(
                    "qm.pipeline.unknown_event",
                    event_type=event.event_type, event_id=str(event.id),
                )
                continue

            try:
                celery_app.send_task(
                    task_name,
                    args=[str(event.id)],
                    queue="ai" if "diagnostic" in task_name else "default",
                )
                dispatched += 1
                logger.info(
                    "qm.pipeline.dispatched",
                    event_type=event.event_type,
                    event_id=str(event.id),
                    task=task_name,
                )
            except Exception as exc:
                failed += 1
                async with session_factory() as db2:
                    await pipeline_service.mark_failed(
                        db2, event_id=event.id, error=f"dispatch: {exc}"
                    )
                    await db2.commit()

    return {"dispatched": dispatched, "failed": failed, "claimed": len(events)}


# ═════════════════════════════════════════════════════════════════════
# 通用 helper · 包装事件处理 + ack
# ═════════════════════════════════════════════════════════════════════

def _run_event_handler(event_id: str, handler_coro_factory):
    """统一事件处理包装：成功 mark_delivered · 失败 mark_failed"""
    return asyncio.run(_run_event_handler_async(event_id, handler_coro_factory))


async def _run_event_handler_async(event_id: str, handler_coro_factory):
    from app.db.session import get_session_factory
    from app.services.qidematrix import pipeline_service

    eid = uuid.UUID(event_id)
    session_factory = get_session_factory()

    async with session_factory() as db:
        event = await pipeline_service.get_event(db, event_id=eid)
        if not event:
            return {"ok": False, "error": "event not found"}
        if event.status == "delivered":
            return {"ok": True, "noop": "already delivered"}

    # 业务逻辑跑（独立 session · 失败 rollback 不影响 status 更新）
    try:
        async with session_factory() as db:
            await handler_coro_factory(db, event)
            await db.commit()
    except Exception as exc:
        async with session_factory() as db:
            await pipeline_service.mark_failed(
                db, event_id=eid, error=str(exc)[:1500]
            )
            await db.commit()
        logger.error("qm.pipeline.handler_failed", event_id=event_id, error=str(exc)[:300])
        raise

    # 标记成功
    async with session_factory() as db:
        await pipeline_service.mark_delivered(db, event_id=eid)
        await db.commit()
    return {"ok": True}


# ═════════════════════════════════════════════════════════════════════
# 14 个事件处理任务 · 全部按统一模式
# ═════════════════════════════════════════════════════════════════════

# ─── S1 ─────────────────────────────────────────────────────────────

@celery_app.task(name="qm.process_onboarding_submitted", queue="default")
def process_onboarding_submitted(event_id: str) -> dict:
    """S1 提交 · 发 welcome 邮件给客户"""
    async def handler(db, event):
        from app.services.qidematrix import email_service, onboarding_service
        ob_id = uuid.UUID(event.payload.get("onboarding_id"))
        ob = await onboarding_service.get_onboarding(db, onboarding_id=ob_id)
        if not ob:
            return
        await email_service.queue_email(
            db,
            tenant_id=ob.tenant_id,
            template_key="welcome",
            locale="zh-CN",
            to_email=ob.contact_email,
            to_name=ob.contact_name,
            template_vars={
                "factory_name": ob.factory_name,
                "contact_name": ob.contact_name,
            },
            onboarding_id=ob.id,
            related_event_id=event.id,
        )
    return _run_event_handler(event_id, handler)


@celery_app.task(name="qm.process_onboarding_completed", queue="default")
def process_onboarding_completed(event_id: str) -> dict:
    """S1 完成 · 触发 S2 诊断（创建 diagnostic 记录）+ S3 DAM workspace"""
    async def handler(db, event):
        from app.services.qidematrix import diagnostic_service, onboarding_service
        ob_id = uuid.UUID(event.payload.get("onboarding_id"))
        ob = await onboarding_service.get_onboarding(db, onboarding_id=ob_id)
        if not ob:
            return
        # 触发 S2 · 创建 diagnostic 行（pending）· 后续 diagnostic.requested 事件被
        # process_diagnostic_requested 接住 · 真跑 LLM
        await diagnostic_service.create_diagnostic_record(db, onboarding=ob)
    return _run_event_handler(event_id, handler)


# ─── S2 ─────────────────────────────────────────────────────────────

@celery_app.task(name="qm.process_diagnostic_requested", queue="ai")
def process_diagnostic_requested(event_id: str) -> dict:
    """S2 跑 LLM 诊断 · 写回 diagnostic 行"""
    async def handler(db, event):
        from app.services.qidematrix import diagnostic_service
        diag_id = uuid.UUID(event.payload.get("diagnostic_id"))
        await diagnostic_service.run_diagnostic(db, diagnostic_id=diag_id)
    return _run_event_handler(event_id, handler)


@celery_app.task(name="qm.process_diagnostic_ready", queue="default")
def process_diagnostic_ready(event_id: str) -> dict:
    """S2 完成 · 触发 PDF 渲染 + diagnostic_ready 邮件 + S3 DAM provisioning"""
    async def handler(db, event):
        diag_id = uuid.UUID(event.payload.get("diagnostic_id"))
        # 触发 PDF 渲染任务（async · 不阻塞）
        celery_app.send_task(
            "qm.render_diagnostic_pdf",
            args=[str(diag_id)],
            queue="media",
        )

        # 同步触发 DAM workspace provisioning
        from app.services.qidematrix import dam_provisioning_service, onboarding_service
        ob_id_str = event.payload.get("onboarding_id")
        if ob_id_str:
            ob = await onboarding_service.get_onboarding(
                db, onboarding_id=uuid.UUID(ob_id_str)
            )
            if ob and not ob.workspace_id:
                # owner_user_id：客户邮箱对应的 user · 找不到时用 placeholder system user
                # 简化版：暂用 onboarding.assigned_operator_id 或 None
                # 生产应该 ensure_customer_user(email) 找/建 user
                from app.services.qidematrix.workspace_service import (
                    _get_or_create_customer_user,
                )
                owner_id = await _get_or_create_customer_user(
                    db, tenant_id=ob.tenant_id,
                    email=ob.contact_email, name=ob.contact_name,
                )
                await dam_provisioning_service.provision_workspace_for_onboarding(
                    db, onboarding=ob, owner_user_id=owner_id,
                )
    return _run_event_handler(event_id, handler)


@celery_app.task(name="qm.process_diagnostic_failed", queue="default")
def process_diagnostic_failed(event_id: str) -> dict:
    """S2 失败 · 通知运营 Sam（不发客户）"""
    async def handler(db, event):
        logger.warning("qm.diagnostic.failed",
                       diagnostic_id=event.payload.get("diagnostic_id"),
                       error=event.payload.get("error"))
        # TODO: 推 Sam 微信 / wecom 提醒
    return _run_event_handler(event_id, handler)


# ─── S3 ─────────────────────────────────────────────────────────────

@celery_app.task(name="qm.process_dam_workspace_ready", queue="default")
def process_dam_workspace_ready(event_id: str) -> dict:
    """S3 完成 · 发 diagnostic_ready 邮件给客户（含 PDF signed URL）"""
    async def handler(db, event):
        from app.services.qidematrix import (
            diagnostic_service, email_service, onboarding_service,
        )
        ob_id_str = event.payload.get("onboarding_id")
        if not ob_id_str:
            return
        ob = await onboarding_service.get_onboarding(
            db, onboarding_id=uuid.UUID(ob_id_str)
        )
        if not ob or not ob.diagnostic_id:
            return
        diag = await diagnostic_service.get_diagnostic(
            db, diagnostic_id=ob.diagnostic_id
        )
        if not diag or diag.status != "ready":
            return

        await email_service.queue_email(
            db,
            tenant_id=ob.tenant_id,
            workspace_id=ob.workspace_id,
            template_key="diagnostic_ready",
            locale="zh-CN",
            to_email=ob.contact_email,
            to_name=ob.contact_name,
            template_vars={
                "factory_name": ob.factory_name,
                "contact_name": ob.contact_name,
                "readiness_score": diag.readiness_score,
                "recommended_path": diag.recommended_plan or "balanced",
                "recommended_plan": diag.recommended_plan or diag.recommended_tier,
                "executive_summary": diag.executive_summary or "",
                "pdf_signed_url": diag.pdf_signed_url or "（PDF 渲染中 · 1 分钟内重新发送）",
            },
            onboarding_id=ob.id,
            diagnostic_id=diag.id,
            related_event_id=event.id,
        )
    return _run_event_handler(event_id, handler)


# ─── S4 ─────────────────────────────────────────────────────────────

@celery_app.task(name="qm.process_social_matrix_requested", queue="default")
def process_social_matrix_requested(event_id: str) -> dict:
    """S4 运营接单 · 推 AI Marketing Lead + 通知 Gavin"""
    async def handler(db, event):
        # 写日志 · UI 显示这个事件即可触发"运营接单中"状态
        logger.info("qm.social.matrix_requested", payload=event.payload)
        # TODO: 推企微 AI Marketing Lead 群 + Gavin/凯岚
    return _run_event_handler(event_id, handler)


@celery_app.task(name="qm.process_social_matrix_ready", queue="default")
def process_social_matrix_ready(event_id: str) -> dict:
    """S4 完成 · social_ready 邮件给客户"""
    async def handler(db, event):
        from app.services.qidematrix import email_service, onboarding_service
        ob_id_str = event.payload.get("onboarding_id")
        if not ob_id_str:
            return
        ob = await onboarding_service.get_onboarding(
            db, onboarding_id=uuid.UUID(ob_id_str)
        )
        if not ob:
            return
        accounts = event.payload.get("accounts", [])
        accounts_list = "\n".join(
            f"• {a.get('platform')} · @{a.get('handle')}" for a in accounts
        )
        await email_service.queue_email(
            db,
            tenant_id=ob.tenant_id,
            workspace_id=ob.workspace_id,
            template_key="social_ready",
            locale="zh-CN",
            to_email=ob.contact_email,
            to_name=ob.contact_name,
            template_vars={
                "factory_name": ob.factory_name,
                "contact_name": ob.contact_name,
                "account_count": len(accounts),
                "accounts_list": accounts_list or "—",
                "content_frequency": event.payload.get("content_frequency", "3-5"),
            },
            onboarding_id=ob.id,
            related_event_id=event.id,
        )
    return _run_event_handler(event_id, handler)


# ─── S5 ─────────────────────────────────────────────────────────────

@celery_app.task(name="qm.process_content_scheduled", queue="default")
def process_content_scheduled(event_id: str) -> dict:
    async def handler(db, event):
        logger.info("qm.content.scheduled", payload=event.payload)
    return _run_event_handler(event_id, handler)


@celery_app.task(name="qm.process_content_published", queue="default")
def process_content_published(event_id: str) -> dict:
    """S5 内容发出 · 累计 content_published_count 到健康度"""
    async def handler(db, event):
        logger.info("qm.content.published", payload=event.payload)
        # 健康度 batch 任务每日跑 · 这里只记日志
    return _run_event_handler(event_id, handler)


# ─── S6 ─────────────────────────────────────────────────────────────

@celery_app.task(name="qm.process_lead_qualified", queue="default")
def process_lead_qualified(event_id: str) -> dict:
    """S6 询盘合格（B/C 类）· 自动报价 / AI 客服回信"""
    async def handler(db, event):
        logger.info("qm.lead.qualified", payload=event.payload)
        # TODO: 触发 quote_service.auto_generate_quote
    return _run_event_handler(event_id, handler)


@celery_app.task(name="qm.process_lead_converted", queue="default")
def process_lead_converted(event_id: str) -> dict:
    """S6 询盘转化（A 类成单）· 发 first_lead 邮件 + 启 S7 派单"""
    async def handler(db, event):
        from app.services.qidematrix import email_service, onboarding_service
        ob_id_str = event.payload.get("onboarding_id")
        if not ob_id_str:
            return
        ob = await onboarding_service.get_onboarding(
            db, onboarding_id=uuid.UUID(ob_id_str)
        )
        if not ob:
            return
        await email_service.queue_email(
            db,
            tenant_id=ob.tenant_id,
            workspace_id=ob.workspace_id,
            template_key="first_lead",
            locale="zh-CN",
            to_email=ob.contact_email,
            to_name=ob.contact_name,
            template_vars={
                "factory_name": ob.factory_name,
                "contact_name": ob.contact_name,
                "buyer_name": event.payload.get("buyer_name", "—"),
                "buyer_country": event.payload.get("buyer_country", "—"),
                "lead_source": event.payload.get("source", "独立站"),
                "lead_grade": event.payload.get("grade", "A"),
                "lead_summary": (event.payload.get("summary") or "")[:200],
            },
            onboarding_id=ob.id,
            related_event_id=event.id,
        )
    return _run_event_handler(event_id, handler)


# ─── S7 ─────────────────────────────────────────────────────────────

@celery_app.task(name="qm.process_order_placed", queue="default")
def process_order_placed(event_id: str) -> dict:
    """S7 订单已派 · 物流推荐"""
    async def handler(db, event):
        logger.info("qm.order.placed", payload=event.payload)
        # TODO: 调 logistics_service 自动生成 logistics_recommendation
    return _run_event_handler(event_id, handler)


@celery_app.task(name="qm.process_order_delivered", queue="default")
def process_order_delivered(event_id: str) -> dict:
    """S7 订单交付完成 · 触发好评请求 + 收款确认提醒"""
    async def handler(db, event):
        logger.info("qm.order.delivered", payload=event.payload)
    return _run_event_handler(event_id, handler)
