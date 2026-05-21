"""QideMatrix · 社媒爆款话题监测 REST API · /v1/qm/topics/* (Phase A)

═══════════════════════════════════════════════════════════════════════
端点：
═══════════════════════════════════════════════════════════════════════
GET    /v1/qm/topics/sources              · 列监测源
PATCH  /v1/qm/topics/sources/{id}          · 启用/停用某监测源（仅 admin）
GET    /v1/qm/topics/candidates            · 列 top N pending 候选
GET    /v1/qm/topics/candidates/{id}       · 单候选详情（含原 Reddit 帖子上下文）
POST   /v1/qm/topics/candidates/{id}/shortlist · 选定该候选 · 进 SEO writer
POST   /v1/qm/topics/candidates/{id}/dismiss   · 否决
GET    /v1/qm/topics/candidates/{id}/seo_context · 给 SEO writer 拼好的 prompt 上下文
POST   /v1/qm/topics/run                   · 手动触发一次完整 pipeline（fetch+score）· admin only

═══════════════════════════════════════════════════════════════════════
鉴权：
═══════════════════════════════════════════════════════════════════════
所有写操作（PATCH / POST）需 workspace 内 admin 或 owner
读操作 · 任何 workspace member 即可
admin 跨 workspace（platform_admin）跑 /run 时可不带 workspace_id
═══════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_current_principal
from app.db.session import get_db
from app.models.qidematrix import (
    QmTopicCandidate,
    QmTopicSignal,
    QmTopicSource,
)
from app.services.qidematrix import (
    topic_monitor_service as tms,
    workspace_service as ws_svc,
)

router = APIRouter(prefix="/qm/topics", tags=["qidematrix-topics"])


def _err(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


# ─── Schemas ────────────────────────────────────────────────────────

class TopicSourceOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID | None
    source_type: str
    source_identifier: str
    display_name: str
    description: str | None
    industry_tags: list[str]
    enabled: bool
    fetch_top_n: int
    fetch_comments_n: int
    fetch_window_hours: int
    last_fetched_at: datetime | None
    last_fetch_count: int | None
    consecutive_failures: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TopicSourcePatchIn(BaseModel):
    enabled: bool | None = None
    fetch_top_n: int | None = Field(default=None, ge=1, le=100)
    fetch_comments_n: int | None = Field(default=None, ge=0, le=50)
    display_name: str | None = None
    description: str | None = None
    industry_tags: list[str] | None = None


class TopicCandidateOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID | None
    signal_id: uuid.UUID
    b2b_relevance: int | None
    search_intent: int | None
    coverage_novelty: int | None
    factory_match: int | None
    composite_score: int | None
    distilled_topic: str | None
    distilled_angle: str | None
    suggested_title: str | None
    suggested_keywords: list[str]
    target_buyer_persona: str | None
    status: str
    shortlisted_at: datetime | None
    dismissed_reason: str | None
    ai_model: str | None
    ai_cost_cny_cents: int
    created_at: datetime

    class Config:
        from_attributes = True


class TopicCandidateDetailOut(TopicCandidateOut):
    signal: dict[str, Any]  # 内联原帖 · 简化前端展示


class DismissIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class RunPipelineIn(BaseModel):
    workspace_id: uuid.UUID | None = None
    skip_fetch: bool = False
    skip_score: bool = False
    score_limit: int = Field(default=200, ge=1, le=1000)


# ─── 鉴权辅助 ────────────────────────────────────────────────────────

async def _assert_workspace_member(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    require_admin: bool = False,
) -> None:
    require_role: tuple[str, ...] = (
        ("owner", "admin") if require_admin else ("owner", "admin", "member", "viewer")
    )
    try:
        await ws_svc.get_workspace_for_user(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            require_role=require_role,
        )
    except ws_svc.WorkspaceNotFound:
        raise _err(404, "workspace not found")
    except ws_svc.WorkspacePermissionDenied as e:
        raise _err(403, str(e))


# ─── Sources ────────────────────────────────────────────────────────

@router.get("/sources", response_model=list[TopicSourceOut])
async def list_sources(
    workspace_id: uuid.UUID = Query(..., description="workspace UUID"),
    enabled_only: bool = Query(False),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    await _assert_workspace_member(db, workspace_id=workspace_id, user_id=p.user_id)

    stmt = select(QmTopicSource).where(QmTopicSource.workspace_id == workspace_id)
    if enabled_only:
        stmt = stmt.where(QmTopicSource.enabled == True)  # noqa: E712
    stmt = stmt.order_by(QmTopicSource.created_at.asc())
    return list((await db.execute(stmt)).scalars().all())


@router.patch("/sources/{source_id}", response_model=TopicSourceOut)
async def patch_source(
    source_id: uuid.UUID,
    payload: TopicSourcePatchIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """启用/停用监测源 · 调整抓取参数 · 仅 admin"""
    if not p.user_id:
        raise _err(401, "user identity required")
    src = (
        await db.execute(select(QmTopicSource).where(QmTopicSource.id == source_id))
    ).scalar_one_or_none()
    if not src:
        raise _err(404, "source not found")
    if not src.workspace_id:
        raise _err(403, "platform source · only platform_admin can edit")
    await _assert_workspace_member(
        db, workspace_id=src.workspace_id, user_id=p.user_id, require_admin=True
    )

    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(src, k, v)
    src.updated_at = datetime.now(tz=src.updated_at.tzinfo if src.updated_at else None)
    await db.commit()
    await db.refresh(src)
    return src


# ─── Candidates ──────────────────────────────────────────────────────

@router.get("/candidates", response_model=list[TopicCandidateOut])
async def list_candidates(
    workspace_id: uuid.UUID = Query(...),
    status: str = Query("pending", description="pending / shortlisted / written / dismissed"),
    top_n: int = Query(10, ge=1, le=100),
    min_score: int = Query(0, ge=0, le=40),
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    await _assert_workspace_member(db, workspace_id=workspace_id, user_id=p.user_id)

    stmt = (
        select(QmTopicCandidate)
        .where(
            QmTopicCandidate.workspace_id == workspace_id,
            QmTopicCandidate.status == status,
        )
        .order_by(QmTopicCandidate.composite_score.desc().nullslast())
        .limit(top_n)
    )
    if min_score > 0:
        stmt = stmt.where(QmTopicCandidate.composite_score >= min_score)
    return list((await db.execute(stmt)).scalars().all())


@router.get("/candidates/{candidate_id}", response_model=TopicCandidateDetailOut)
async def get_candidate(
    candidate_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    cand = (
        await db.execute(
            select(QmTopicCandidate).where(QmTopicCandidate.id == candidate_id)
        )
    ).scalar_one_or_none()
    if not cand:
        raise _err(404, "candidate not found")
    if cand.workspace_id:
        await _assert_workspace_member(
            db, workspace_id=cand.workspace_id, user_id=p.user_id
        )

    signal = (
        await db.execute(
            select(QmTopicSignal).where(QmTopicSignal.id == cand.signal_id)
        )
    ).scalar_one_or_none()
    signal_view = (
        {
            "id": str(signal.id),
            "external_url": signal.external_url,
            "title": signal.title,
            "body": (signal.body or "")[:2000],
            "author_handle": signal.author_handle,
            "score": signal.score,
            "num_comments": signal.num_comments,
            "top_comments": list(signal.top_comments or [])[:5],
            "posted_at": signal.posted_at.isoformat() if signal.posted_at else None,
        }
        if signal
        else {}
    )

    return TopicCandidateDetailOut(
        **{k: getattr(cand, k) for k in TopicCandidateOut.model_fields},
        signal=signal_view,
    )


@router.post(
    "/candidates/{candidate_id}/shortlist", response_model=TopicCandidateOut
)
async def shortlist_candidate(
    candidate_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """Sam 选定该话题进 SEO writer · status: pending → shortlisted"""
    if not p.user_id:
        raise _err(401, "user identity required")
    cand = (
        await db.execute(
            select(QmTopicCandidate).where(QmTopicCandidate.id == candidate_id)
        )
    ).scalar_one_or_none()
    if not cand:
        raise _err(404, "candidate not found")
    if cand.workspace_id:
        await _assert_workspace_member(
            db, workspace_id=cand.workspace_id, user_id=p.user_id, require_admin=True
        )

    try:
        result = await tms.shortlist_candidate(
            db, candidate_id=candidate_id, user_id=p.user_id
        )
    except ValueError as e:
        raise _err(400, str(e))
    return result


@router.post(
    "/candidates/{candidate_id}/dismiss", response_model=TopicCandidateOut
)
async def dismiss_candidate(
    candidate_id: uuid.UUID,
    payload: DismissIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    if not p.user_id:
        raise _err(401, "user identity required")
    cand = (
        await db.execute(
            select(QmTopicCandidate).where(QmTopicCandidate.id == candidate_id)
        )
    ).scalar_one_or_none()
    if not cand:
        raise _err(404, "candidate not found")
    if cand.workspace_id:
        await _assert_workspace_member(
            db, workspace_id=cand.workspace_id, user_id=p.user_id, require_admin=True
        )

    try:
        result = await tms.dismiss_candidate(
            db, candidate_id=candidate_id, reason=payload.reason
        )
    except ValueError as e:
        raise _err(400, str(e))
    return result


@router.get("/candidates/{candidate_id}/seo_context")
async def get_seo_context(
    candidate_id: uuid.UUID,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """给 SEO writer 拼好的上下文 prompt（含原 Reddit 帖子 + AI 提炼角度）

    Phase B 整合点：SEO writer 在拿 shortlist 后调这个端点拿上下文 · 塞进 LLM prompt
    """
    if not p.user_id:
        raise _err(401, "user identity required")
    cand = (
        await db.execute(
            select(QmTopicCandidate).where(QmTopicCandidate.id == candidate_id)
        )
    ).scalar_one_or_none()
    if not cand:
        raise _err(404, "candidate not found")
    if cand.workspace_id:
        await _assert_workspace_member(
            db, workspace_id=cand.workspace_id, user_id=p.user_id
        )

    signal = (
        await db.execute(
            select(QmTopicSignal).where(QmTopicSignal.id == cand.signal_id)
        )
    ).scalar_one_or_none()
    if not signal:
        raise _err(404, "signal not found")

    context_text = tms.build_seo_writer_context(cand, signal)
    return {
        "candidate_id": str(cand.id),
        "signal_id": str(signal.id),
        "status": cand.status,
        "suggested_title": cand.suggested_title,
        "suggested_keywords": list(cand.suggested_keywords or []),
        "target_buyer_persona": cand.target_buyer_persona,
        "context_prompt": context_text,
    }


# ─── Manual pipeline trigger ──────────────────────────────────────────

@router.post("/run")
async def run_pipeline(
    payload: RunPipelineIn,
    p: Principal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
):
    """手动跑一次：fetch + score + top 候选 · 用于排查或 Sam 想立刻看新候选

    限制：
      · 需 platform_admin（跨 workspace）· 或限定 workspace_id 且为该 ws admin
      · 同步等待 · 大量 LLM 调用会慢 · 建议 score_limit 限制
    """
    if not p.user_id:
        raise _err(401, "user identity required")
    is_platform = bool(getattr(p, "is_platform_admin", False))

    ws_uuid: uuid.UUID | None = payload.workspace_id
    if ws_uuid is not None:
        await _assert_workspace_member(
            db, workspace_id=ws_uuid, user_id=p.user_id, require_admin=True
        )
    else:
        if not is_platform:
            raise _err(403, "全 workspace 触发仅 platform_admin")

    fetch_result: dict[str, Any] = {"skipped": True}
    score_result: dict[str, Any] = {"skipped": True}

    if not payload.skip_fetch:
        fetch_result = await tms.fetch_and_store_all_enabled(
            db, workspace_id=ws_uuid
        )

    if not payload.skip_score:
        score_result = await tms.score_unscored_signals(
            db, workspace_id=ws_uuid, limit=payload.score_limit
        )

    top = await tms.list_top_pending_candidates(
        db, workspace_id=ws_uuid, top_n=3
    )
    top_summary = [
        {
            "id": str(c.id),
            "score": c.composite_score,
            "title": c.suggested_title or c.distilled_topic,
            "keywords": list(c.suggested_keywords or []),
        }
        for c in top
    ]

    return {
        "fetch": fetch_result,
        "score": score_result,
        "top_candidates": top_summary,
    }
