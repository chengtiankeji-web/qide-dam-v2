"""Consolidate · 把 handover / plans 散文档消化到 memory · v3 P1.3 #5 (2026-05-13 晚)

需求来源：Sam 2026-05-13 晚拍板 ·
"handover 里面加一个手动按钮 · 功能是：把有效信息消化整理进 memory ·
 历史旧版本或者无效信息删掉 · plans 同理"

设计：
  POST /v1/consolidate/preview
    Body: { scope: "handover"|"plans"|"sources", project_id, target_memory_name? }
    → 列出该 scope 命中的文件 + 用 qwen-plus 生成"建议 memory.md 内容"
    返回 { proposed_content, source_asset_ids, model, token_estimate }
    用户在 admin SPA 修改预览 · 满意才 Apply

  POST /v1/consolidate/apply
    Body: { project_id, memory_filename, content, archive_source_ids? }
    → 上传 content 为新 memory asset · soft-delete archive_source_ids
    返回 { new_asset_id, archived_count }
    写 audit event 每一步留痕
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.asset import Asset
from app.schemas.asset import PresignedUploadIn
from app.services import ai_service, asset_service, audit_service, storage
from app.services.audit_service import AuditAction

router = APIRouter()

# ─── 各 scope 文件名 / tag 命中规则 ────────────────────────────────
SCOPE_PATTERNS = {
    "handover": {
        "tag_regex": r"handover|交接|sop|runbook",
        "name_regex": r"handover|交接|sop|runbook",
        "target_memory_default": "handover-consolidated.md",
        "system_prompt": (
            "你是 Sam 的运营记忆官。Sam 是祁德商链科技 CTO · 跨 7 项目 · 信息密度高。"
            "下面是 Sam 工作空间里 handover 类文档堆积（交接 / SOP / runbook / 路演脚本等）·"
            "请把所有有用信息精炼整合成一份 memory · markdown 格式 · "
            "保留：决策 / 联系人 / 凭证位置（不抄具体凭证）/ 数字事实 / 时间线。"
            "去掉：(a) 已过期的旧版本（同一个流程的早期 draft）"
            "(b) 重复的临时操作日志（这次部署完成后的 changelog 转 1 行总结）"
            "(c) 调试时的失败尝试（保留最终方案）。"
            "输出长度：原文密度的 1/5 ~ 1/3 之间。中文 · markdown · 顶部 H1 = 'Handover · 精炼后的 memory'。"
        ),
    },
    "plans": {
        "tag_regex": r"plan|roadmap|sprint|todo|计划|路线图",
        "name_regex": r"plan|roadmap|todo|计划",
        "target_memory_default": "plans-consolidated.md",
        "system_prompt": (
            "你是 Sam 的运营记忆官。下面是 Sam 工作空间里 plans / roadmap / sprint plan / todo 类文档堆积·"
            "请合并成一份当前生效的 plans memory · markdown 格式 · "
            "保留：未完成 todo / 当前 sprint / 下季度 roadmap / 长期愿景。"
            "去掉：已完成的 todo（除非有总结性价值）/ 失效 roadmap / 临时草稿。"
            "输出：markdown · 顶部 H1 = 'Plans · 当前在做 + 即将做'。"
        ),
    },
    "sources": {
        "tag_regex": r"sources|原件|资料|intake",
        "name_regex": r"sources|资料",
        "target_memory_default": "sources-consolidated.md",
        "system_prompt": (
            "请用 1 段话总结这些客户资料 / 工厂 intake 文档的核心信息（公司名 / 联系人 / 工厂能力 / 价格区间等）·"
            "markdown 列表 · 每个原件 1-3 行 bullet · 不抄具体证书号 / 不抄电话。"
        ),
    },
}

# ─── 单文件最大 fetch 大小（防把超大文档全塞 LLM） ─────────────────
MAX_PER_FILE_BYTES = 200 * 1024  # 200 KB
# v3 P1.3 三修 (2026-05-13 深夜): 之前 qwen-plus 默认 32K context · 我塞 300KB ≈ 100K tokens 直接 400 ·
# 换 qwen-long (1M tokens · 远够) · 大文档场景永远不超 context · max input 安全降到 200 KB（约 60K tokens）·
# qwen-long 输出 max_tokens=8K 也比 qwen-plus 大
MAX_TOTAL_INPUT_BYTES = 200 * 1024  # ~60K tokens for Chinese · qwen-long 接收 ≤1M tokens 永远够
CONSOLIDATE_MODEL = "qwen-long"     # DashScope: 1M tokens context · 文档总结场景首选


class ConsolidatePreviewIn(BaseModel):
    scope: Literal["handover", "plans", "sources"]
    # v3 P1.3 (2026-05-13 晚二修): project_id 改可选 ·
    # None = 跨整个 tenant 找匹配 scope 的资产（Sam 反馈 CMH 下点 handover 0 命中 ·
    # 因为 handover 都在 qidematrix-sam · 跨 tenant 查最合理）
    project_id: uuid.UUID | None = None
    # 可选：限定 specific asset_ids（不传 = 自动按 scope 命中规则全收）
    asset_ids: list[uuid.UUID] | None = None
    target_memory_name: str | None = None
    # 输出 memory 文件落到哪个 project · None = 跟 project_id 一致 · 都 None 时落 qidematrix-sam（默认 memory 项目）
    output_project_id: uuid.UUID | None = None


class ConsolidatePreviewOut(BaseModel):
    scope: str
    proposed_content: str
    proposed_memory_name: str
    source_asset_ids: list[uuid.UUID]
    source_total_bytes: int
    truncated: bool
    model: str


class ConsolidateApplyIn(BaseModel):
    project_id: uuid.UUID
    memory_filename: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=10)
    archive_source_ids: list[uuid.UUID] | None = None


class ConsolidateApplyOut(BaseModel):
    new_asset_id: uuid.UUID
    new_asset_name: str
    archived_count: int


async def _list_scope_assets(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    scope: str,
) -> list[Asset]:
    """按 scope tag / name 规则筛选出匹配的资产。

    v3 P1.3 (2026-05-13 晚二修): project_id=None 时跨整个 tenant 查 ·
    项目级别 consolidate 才传 project_id · 跨 project 场景（handover/plans/sources）None 最合理。

    简化：先一次性 list 所有 status=ready · markdown/text/document kind · 然后 Python 端 regex 过滤。
    """
    import re

    stmt = (
        select(Asset)
        .where(
            Asset.tenant_id == tenant_id,
            Asset.deleted_at.is_(None),
            Asset.status == "ready",
            Asset.kind.in_(("document", "other")),
        )
        .order_by(Asset.updated_at.desc())
        .limit(500)
    )
    if project_id:
        stmt = stmt.where(Asset.project_id == project_id)
    rows = (await db.execute(stmt)).scalars().all()

    pat = SCOPE_PATTERNS.get(scope, {})
    tag_re = re.compile(pat.get("tag_regex", "$.^"), re.IGNORECASE)
    name_re = re.compile(pat.get("name_regex", "$.^"), re.IGNORECASE)

    return [
        a for a in rows
        if any(tag_re.search(t) for t in (a.manual_tags or []) + (a.auto_tags or []))
        or name_re.search(a.name or "")
    ]


def _fetch_text_safe(storage_key: str, mime_type: str | None) -> str | None:
    """从 R2 安全取文本 · 截断 · 失败返 None"""
    text_mimes = {
        "text/markdown", "text/plain", "text/csv", "text/x-markdown",
        "application/json", "application/yaml", "application/x-yaml",
        "text/html", "application/xml",
    }
    if mime_type and mime_type not in text_mimes and not mime_type.startswith("text/"):
        return None
    try:
        body = storage.get_object(storage_key)
        if len(body) > MAX_PER_FILE_BYTES:
            body = body[:MAX_PER_FILE_BYTES]
        return body.decode("utf-8", errors="replace")
    except Exception:
        return None


@router.post("/preview", response_model=ConsolidatePreviewOut)
async def consolidate_preview(
    payload: ConsolidatePreviewIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> ConsolidatePreviewOut:
    """v3 P1.3 #5 · 生成 memory.md 候选 · 用户在 admin SPA 看到后改/拒/应用

    v3 P1.3 二修 (2026-05-13 晚): project_id 改可选 · None = 跨 tenant
    """
    if payload.project_id and not p.can_access_project(payload.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")

    pat = SCOPE_PATTERNS.get(payload.scope)
    if not pat:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown scope: {payload.scope}")

    # 命中文件
    if payload.asset_ids:
        clauses = [
            Asset.id.in_(payload.asset_ids),
            Asset.tenant_id == p.tenant_id,
            Asset.deleted_at.is_(None),
        ]
        if payload.project_id:
            clauses.append(Asset.project_id == payload.project_id)
        stmt = select(Asset).where(*clauses)
        assets = list((await db.execute(stmt)).scalars().all())
    else:
        assets = await _list_scope_assets(
            db, tenant_id=p.tenant_id, project_id=payload.project_id, scope=payload.scope
        )

    if not assets:
        return ConsolidatePreviewOut(
            scope=payload.scope,
            proposed_content=f"# {payload.scope.title()}\n\n_无匹配文档_",
            proposed_memory_name=payload.target_memory_name or pat["target_memory_default"],
            source_asset_ids=[],
            source_total_bytes=0,
            truncated=False,
            model="none",
        )

    # 拉文本（截到 MAX_TOTAL_INPUT_BYTES 总量）
    # v3 P1.3 (2026-05-13 晚) 修：按字节切而非字符切 · 中文 UTF-8 多字节场景不漏算
    sources_text: list[str] = []
    used_asset_ids: list[uuid.UUID] = []
    total_bytes = 0
    truncated = False
    for a in assets:
        if total_bytes >= MAX_TOTAL_INPUT_BYTES:
            truncated = True
            break
        body = _fetch_text_safe(a.storage_key, a.mime_type)
        if not body:
            continue
        block_header = f"\n\n--- file: {a.name} (asset_id={a.id}, {a.size_bytes}B) ---\n"
        block = block_header + body
        block_bytes = block.encode("utf-8")
        if total_bytes + len(block_bytes) > MAX_TOTAL_INPUT_BYTES:
            remaining = MAX_TOTAL_INPUT_BYTES - total_bytes
            # 真按字节切 + utf-8 容错（防多字节字符切到一半）
            block_bytes = block_bytes[:remaining]
            block = block_bytes.decode("utf-8", errors="ignore")
            truncated = True
        sources_text.append(block)
        used_asset_ids.append(a.id)
        total_bytes += len(block.encode("utf-8"))

    combined = "".join(sources_text)
    prompt = (
        f"以下是 {len(used_asset_ids)} 份 {payload.scope} 类文档原文 ·\n"
        f"按上方 system 指示精炼成 memory.md：\n\n{combined}"
    )

    # 调 ai_service.text_gen_for · use_case=long_doc_consolidate ·
    # 自动按 USE_CASE_MODELS 走 qwen-long → qwen-plus → qwen3.6-flash 三级 fallback
    try:
        proposed = ai_service.text_gen_for(
            "long_doc_consolidate",
            prompt,
            system=pat["system_prompt"],
            temperature=0.3,  # 偏低 · 更稳定
        )
        model = CONSOLIDATE_MODEL  # 报告里显示首选模型 · 实际跑的模型在 log
    except Exception as exc:  # noqa: BLE001
        proposed = f"# {payload.scope.title()} · 精炼候选\n\n_LLM 调用失败: {exc!s}_\n\n## 源文件列表（fallback）\n\n" + "\n".join(
            f"- {a.name} ({a.size_bytes}B)" for a in assets if a.id in used_asset_ids
        )
        model = "stub_fallback"

    return ConsolidatePreviewOut(
        scope=payload.scope,
        proposed_content=proposed,
        proposed_memory_name=payload.target_memory_name or pat["target_memory_default"],
        source_asset_ids=used_asset_ids,
        source_total_bytes=total_bytes,
        truncated=truncated,
        model=model,
    )


@router.post("/apply", response_model=ConsolidateApplyOut)
async def consolidate_apply(
    payload: ConsolidateApplyIn,
    request: Request,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> ConsolidateApplyOut:
    """v3 P1.3 #5 · 应用消化结果 · 上传 memory.md + 可选 archive 源文件"""
    if not p.can_access_project(payload.project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")

    # 1. 上传 content 为新 memory asset
    content_bytes = payload.content.encode("utf-8")
    sha = hashlib.sha256(content_bytes).hexdigest()

    new_asset_payload = PresignedUploadIn(
        project_id=payload.project_id,
        filename=payload.memory_filename,
        mime_type="text/markdown",
        size_bytes=len(content_bytes),
        sha256=sha,
        acl="project",
        manual_tags=["memory", "consolidated", "auto-generated"],
    )
    asset, upload_url, headers, deduplicated = await asset_service.register_presigned_upload(
        db,
        tenant_id=p.tenant_id,
        payload=new_asset_payload,
        dedup_strategy="link",  # 同 sha 已存在就直接 link · 不重复
    )

    # 如果没 dedup · 直接 PUT 内容到 R2 + confirm
    if not deduplicated and upload_url:
        import requests as _req
        put = _req.put(upload_url, data=content_bytes,
                       headers={"Content-Type": "text/markdown", **(headers or {})},
                       timeout=30)
        if put.status_code not in (200, 201, 204):
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"R2 PUT failed: HTTP {put.status_code}",
            )
        await asset_service.confirm_upload(db, tenant_id=p.tenant_id, asset_id=asset.id)

    # 2. archive sources
    archived = 0
    if payload.archive_source_ids:
        for sid in payload.archive_source_ids:
            try:
                await asset_service.soft_delete_asset(
                    db, tenant_id=p.tenant_id, asset_id=sid
                )
                archived += 1
            except Exception:  # noqa: BLE001
                pass

    # 3. audit
    await audit_service.audit(
        db,
        action=AuditAction.ASSET_CREATED,
        tenant_id=p.tenant_id,
        project_id=payload.project_id,
        actor_user_id=p.user_id,
        actor_kind="user" if p.via == "jwt" else "api_key",
        target_kind="asset",
        target_id=asset.id,
        request=request,
        metadata={
            "operation": "consolidate_apply",
            "memory_filename": payload.memory_filename,
            "archived_source_count": archived,
            "content_bytes": len(content_bytes),
        },
    )

    await db.commit()
    return ConsolidateApplyOut(
        new_asset_id=asset.id,
        new_asset_name=asset.name,
        archived_count=archived,
    )
