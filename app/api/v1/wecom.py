"""WeCom API endpoints · v0.1
- POST /v1/wecom/send-asset/{asset_id}     · 把 asset 发到指定企微人 (内部触发)
- GET  /v1/wecom/users                      · list 通讯录
- GET  /v1/wecom/resolve?hint=刘总          · 模糊匹配联系人
- GET  /v1/wecom/callback                   · 企微回调验证 (echostr)  [Phase B]
- POST /v1/wecom/callback                   · 接收用户消息            [Phase B]
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.services import asset_service, storage, wecom_service

router = APIRouter()


@router.get("/users")
async def list_wecom_users(p: Principal = Depends(get_current_principal)) -> list[dict]:
    if not p.is_platform_admin and p.role not in {"tenant_admin", "platform_admin"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    if not settings.WECOM_CORPID:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "WECOM 未配置")
    return await wecom_service.list_users()


@router.get("/resolve")
async def resolve_user(
    hint: str = Query(..., min_length=1, description="联系人名称提示 · 如 '刘总' / 'Sam'"),
    p: Principal = Depends(get_current_principal),
) -> dict:
    if not p.is_platform_admin and p.role not in {"tenant_admin", "platform_admin"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    user = await wecom_service.resolve_user_by_name(hint)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"没找到 {hint!r}")
    return {
        "userid": user.get("userid"),
        "name": user.get("name"),
        "position": user.get("position"),
        "department": user.get("department"),
    }


@router.post("/send-asset/{asset_id}")
async def send_asset_to_user(
    asset_id: uuid.UUID,
    recipient: str = Query(..., description="touser · 企微 userid 或 '@all'"),
    note: str | None = Query(None, description="附带文本说明 · 可选"),
    expires_in: int = Query(86400, ge=60, le=86400 * 7),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Send a DAM asset to a WeCom user/group as a clickable file card."""
    if not settings.WECOM_CORPID:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "WECOM 未配置")

    # platform_admin 跨 tenant: 反查 asset 的真 tenant_id
    from sqlalchemy import select

    from app.models.asset import Asset as _A
    if p.is_platform_admin:
        row = (await db.execute(select(_A).where(_A.id == asset_id))).scalar_one_or_none()
        effective_tid = row.tenant_id if row else p.tenant_id
    else:
        effective_tid = p.tenant_id

    try:
        asset = await asset_service.get_asset(db, tenant_id=effective_tid, asset_id=asset_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    if not p.can_access_project(asset.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")

    url = storage.presign_get(storage_key=asset.storage_key, expires_in=expires_in)

    # 可选 · 先发文字说明再发文件卡
    if note:
        await wecom_service.send_text(recipient, note)

    res = await wecom_service.send_file_link(
        recipient,
        filename=asset.name,
        size_bytes=asset.size_bytes,
        url=url,
        description=asset.ai_summary or asset.description,
    )
    return {
        "ok": res.get("errcode", 0) == 0,
        "wecom_response": res,
        "asset_id": str(asset.id),
        "filename": asset.name,
        "url": url,
    }


@router.post("/send-text")
async def send_text_message(
    recipient: str = Query(..., description="touser"),
    content: str = Query(..., min_length=1, max_length=2000),
    p: Principal = Depends(get_current_principal),
) -> dict:
    if not settings.WECOM_CORPID:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "WECOM 未配置")
    if not p.is_platform_admin and p.role not in {"tenant_admin", "platform_admin"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    res = await wecom_service.send_text(recipient, content)
    return {"ok": res.get("errcode", 0) == 0, "wecom_response": res}


# ───── Callback (Phase B · 真实解密) ─────

from fastapi import Request
from fastapi.responses import PlainTextResponse, Response

from app.services import wecom_crypto


@router.get("/callback", response_class=PlainTextResponse)
async def callback_verify(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
) -> str:
    """企微 GET 请求验证 URL · SHA1 校验签名 + AES 解密 echostr 返回明文。"""
    if not settings.WECOM_CALLBACK_TOKEN or not settings.WECOM_CALLBACK_AESKEY:
        raise HTTPException(503, "WECOM_CALLBACK_TOKEN/AESKEY 未配置 · 加 .env 后重启")
    if not wecom_crypto.verify_signature(
        settings.WECOM_CALLBACK_TOKEN, timestamp, nonce, echostr, msg_signature
    ):
        raise HTTPException(401, "signature mismatch")
    try:
        plain, corpid = wecom_crypto.aes_decrypt(settings.WECOM_CALLBACK_AESKEY, echostr)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"decrypt failed: {e}")
    if settings.WECOM_CORPID and corpid != settings.WECOM_CORPID:
        raise HTTPException(401, "corpid mismatch")
    return plain  # 返回明文 echostr · 企微会校验


@router.post("/callback")
async def callback_message(
    request: Request,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
):
    """企微 POST 推送用户消息 · 解密 → 异步处理 → 立刻返回空响应（< 5s）。
    v0.1 先实现解析 + 简单回声 · 后续接 AI 意图解析 + DAM 搜 + 转发文件。"""
    if not settings.WECOM_CALLBACK_TOKEN or not settings.WECOM_CALLBACK_AESKEY:
        raise HTTPException(503, "WECOM_CALLBACK 未配置")
    body = (await request.body()).decode("utf-8", errors="replace")

    import re
    m = re.search(r"<Encrypt>\s*<!\[CDATA\[(.+?)\]\]>\s*</Encrypt>", body)
    if not m:
        raise HTTPException(400, "no <Encrypt> in body")
    encrypted = m.group(1)

    if not wecom_crypto.verify_signature(
        settings.WECOM_CALLBACK_TOKEN, timestamp, nonce, encrypted, msg_signature
    ):
        raise HTTPException(401, "signature mismatch")

    try:
        xml, corpid = wecom_crypto.aes_decrypt(settings.WECOM_CALLBACK_AESKEY, encrypted)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"decrypt failed: {e}")

    # 解析 XML 提取 FromUserName + Content
    from_user = re.search(r"<FromUserName>\s*<!\[CDATA\[(.+?)\]\]>", xml)
    content_m = re.search(r"<Content>\s*<!\[CDATA\[(.+?)\]\]>", xml)
    msg_type_m = re.search(r"<MsgType>\s*<!\[CDATA\[(.+?)\]\]>", xml)
    sender = from_user.group(1) if from_user else "unknown"
    text = content_m.group(1) if content_m else ""
    msg_type = msg_type_m.group(1) if msg_type_m else ""

    from app.core.logging import get_logger
    log = get_logger("wecom.callback")
    log.info("wecom.message.received", sender=sender, msg_type=msg_type, content=text[:200])

    # v0.1 · 收到消息后异步处理：调 AI 意图 → DAM 搜 → 发文件
    if msg_type == "text" and text:
        import asyncio
        asyncio.create_task(_handle_user_query(sender, text))

    return Response(status_code=200)  # 企微要求快速 200 返回


async def _handle_user_query(sender: str, text: str):
    """v0.1 · 简单关键字 · 之后接 DashScope qwen-plus 意图解析"""
    import re

    from app.db.session import AsyncSessionLocal
    from app.services import asset_service, storage

    text_low = text.strip()

    # 简单意图：找"发给X"
    recipient_hint = None
    m = re.search(r"发给\s*([一-龥A-Za-z]{1,10})", text_low)
    if m:
        recipient_hint = m.group(1)

    # 提取 query (去掉指令词)
    query = re.sub(r"把\s*|发给\s*\S+|发到\s*\S+|[?？！。]", "", text_low).strip()
    query = re.sub(r"^(找|搜|查|看|帮我)\s*", "", query).strip()
    if not query:
        await wecom_service.send_text(sender, "我没听懂 · 试试『把最新报价单发给刘总』这种")
        return

    # DAM 搜
    async with AsyncSessionLocal() as db:
        # 先用 qide tenant 的 Qide-Cowork project（写死 v0.1）
        from sqlalchemy import select

        from app.models.tenant import Tenant
        t = (await db.execute(select(Tenant).where(Tenant.slug == "qide"))).scalar_one_or_none()
        if not t:
            await wecom_service.send_text(sender, "❌ 找不到 qide tenant")
            return
        items, total = await asset_service.list_assets(
            db, tenant_id=t.id, project_id=None, kind=None, status=None,
            q=query, page=1, page_size=5,
        )

    if not items:
        await wecom_service.send_text(sender, f"❌ 没找到 「{query}」 相关的文件")
        return

    # 解析收件人
    target_userid = sender  # 默认发给自己
    if recipient_hint:
        target = await wecom_service.resolve_user_by_name(recipient_hint)
        if target:
            target_userid = target.get("userid")
        else:
            await wecom_service.send_text(sender, f"⚠️ 没找到联系人「{recipient_hint}」 · 文件先发你自己")
            target_userid = sender

    # 取第一个（最匹配的）
    asset = items[0]
    url = storage.presign_get(storage_key=asset.storage_key, expires_in=86400)
    await wecom_service.send_file_link(
        target_userid,
        filename=asset.name,
        size_bytes=asset.size_bytes,
        url=url,
        description=asset.ai_summary or asset.description,
    )

    # 给发起人回复确认
    if target_userid == sender:
        await wecom_service.send_text(sender, f"✅ 已发文件「{asset.name}」 (共 {total} 个匹配)")
    else:
        target_name = target.get("name", target_userid) if recipient_hint else target_userid
        await wecom_service.send_text(sender, f"✅ 已把「{asset.name}」发给 {target_name}")
