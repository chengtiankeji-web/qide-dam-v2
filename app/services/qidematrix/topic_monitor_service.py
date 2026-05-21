"""话题监测核心业务 · Phase A

主流程（每天 06:00 CST 跑）：
  1. 列 enabled qm_topic_sources（默认 7 个 subreddit）
  2. 每个 source 调 Reddit API 抓 top 20 posts + top 10 comments
  3. 落 qm_topic_signals · 防重（external_id 唯一）
  4. 新 signals 调 LLM 用 lead_classify 路由打分
  5. 综合分 >= 28/40 的进 candidates · status=pending
  6. 返回 top N 候选 · 上层（scheduled task / API）推 Sam 微信
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.qidematrix import (
    QmTopicCandidate,
    QmTopicSignal,
    QmTopicSource,
)
from app.services import ai_service
from app.services.qidematrix.reddit_client import (
    RedditClient,
    RedditError,
    RedditPost,
    get_reddit_client,
)

logger = get_logger(__name__)


# 综合分阈值 · 满分 40 (4 维度 × 10) · 28 = 70% 算"高质量"
SCORE_THRESHOLD = 28


SCORING_SYSTEM_PROMPT = """你是中国出口制造业 SEO 选题分析师。你看到的是从 Reddit 等社媒抓的话题。
你的任务是给每个话题按 4 个维度打分（每项 0-10），然后输出一句话总结 + SEO 文章切入角度建议。

4 个评分维度：

1. **b2b_relevance** · B2B 相关性
   · 10 = 帖子直接讨论"找工厂/供应链/OEM/代工/B2B 采购"
   · 5 = 偶尔提到 · 但不是主话题
   · 0 = 纯 C 端 / 跟 B2B 无关

2. **search_intent** · 搜索意图强度
   · 10 = 帖子表达"我在搜 / 求推荐 / 怎么选"明确商业意图
   · 5 = 经验分享 · 别人可能会因为类似问题搜
   · 0 = 闲聊 / 抱怨 / 不会有人搜

3. **coverage_novelty** · 跟既有 SEO 文章不重叠度
   · 10 = 全新角度 · 我们没写过类似话题
   · 5 = 相关但角度不同
   · 0 = 我们已经写过同样话题

4. **factory_match** · 跟祁德服务的中国出口工厂匹配度
   · 10 = 帖子里的需求/痛点正是中国工厂能解决的
   · 5 = 部分匹配
   · 0 = 完全不匹配（如本土制造 / 服务业 / 软件）

输出格式严格 JSON · 不要其它文字：
{
  "b2b_relevance": 0-10,
  "search_intent": 0-10,
  "coverage_novelty": 0-10,
  "factory_match": 0-10,
  "distilled_topic": "<50 字 · 这话题的核心命题>",
  "distilled_angle": "<100 字 · 我们这个外贸工厂角度该怎么写 · 谁是读者 · 主张什么>",
  "suggested_title": "<英文 · 50-70 字 · 直接可用的 SEO 文章标题>",
  "suggested_keywords": ["3-5 个英文关键词"],
  "target_buyer_persona": "<30 字 · 哪类买家会读这文章>"
}
"""


def _build_scoring_prompt(post: RedditPost, source_name: str) -> str:
    """把 Reddit post + comments 拼成 LLM 输入"""
    comments_text = "\n".join(
        f"  - [{c.get('score', 0)}↑] {c.get('author', '?')}: {c.get('body', '')[:400]}"
        for c in (post.top_comments or [])[:6]
    )
    return f"""【来源】{source_name}（Reddit）
【帖子 {post.score}↑ · {post.num_comments} 评论】
{post.title}

{post.body[:1500]}

【Top 评论】
{comments_text or '（无）'}

请按 system prompt 输出 JSON 评分。
"""


# ─── 抓取 + 入库 ─────────────────────────────────────────────────────

async def fetch_and_store_source(
    db: AsyncSession,
    source: QmTopicSource,
    *,
    reddit_client: RedditClient | None = None,
) -> tuple[int, int]:
    """抓一个监测源 · 返 (新增 signals 数, 总抓取数)

    幂等：external_id 已存在则跳过
    """
    if source.source_type != "reddit":
        logger.info("topic_monitor.source.skip_unsupported", type=source.source_type)
        return 0, 0

    client = reddit_client or get_reddit_client()
    try:
        posts = await client.fetch_subreddit_full(
            source.source_identifier,
            posts_limit=source.fetch_top_n,
            comments_limit=source.fetch_comments_n,
        )
    except RedditError as e:
        logger.warning(
            "topic_monitor.fetch_failed",
            source=source.source_identifier, error=str(e)[:200],
        )
        source.consecutive_failures += 1
        source.updated_at = datetime.now(UTC)
        await db.flush()
        return 0, 0

    now = datetime.now(UTC)
    new_count = 0

    for post in posts:
        # 防重
        existing = (
            await db.execute(
                select(QmTopicSignal).where(
                    QmTopicSignal.source_id == source.id,
                    QmTopicSignal.external_id == post.external_id,
                )
            )
        ).scalar_one_or_none()
        if existing:
            continue

        signal = QmTopicSignal(
            id=uuid.uuid4(),
            workspace_id=source.workspace_id,
            source_id=source.id,
            external_id=post.external_id,
            external_url=post.url,
            title=post.title,
            body=post.body,
            author_handle=post.author,
            score=post.score,
            num_comments=post.num_comments,
            top_comments=post.top_comments,
            posted_at=datetime.fromtimestamp(post.posted_at_ts, tz=UTC) if post.posted_at_ts else None,
            fetched_at=now,
            extra_metadata={},
        )
        db.add(signal)
        new_count += 1

    source.last_fetched_at = now
    source.last_fetch_count = len(posts)
    source.consecutive_failures = 0
    source.updated_at = now
    await db.flush()

    logger.info(
        "topic_monitor.fetched",
        source=source.source_identifier,
        total=len(posts), new=new_count,
    )
    return new_count, len(posts)


async def fetch_and_store_all_enabled(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID | None = None,
) -> dict:
    """抓全部 enabled 监测源 · workspace_id=None 则全 workspace（限 admin 用）"""
    stmt = select(QmTopicSource).where(QmTopicSource.enabled == True)  # noqa: E712
    if workspace_id:
        stmt = stmt.where(QmTopicSource.workspace_id == workspace_id)
    sources = (await db.execute(stmt)).scalars().all()

    totals = {"sources_processed": 0, "new_signals": 0, "total_fetched": 0, "errors": 0}
    client = get_reddit_client()
    for src in sources:
        try:
            new_n, total_n = await fetch_and_store_source(db, src, reddit_client=client)
            totals["new_signals"] += new_n
            totals["total_fetched"] += total_n
            totals["sources_processed"] += 1
        except Exception as e:  # noqa: BLE001
            logger.error(
                "topic_monitor.source.exception",
                source=src.source_identifier, error=str(e)[:200],
            )
            totals["errors"] += 1
    await db.commit()
    return totals


# ─── LLM 评分 ────────────────────────────────────────────────────────

async def score_signal(
    db: AsyncSession,
    signal: QmTopicSignal,
    source: QmTopicSource,
) -> QmTopicCandidate | None:
    """对 1 个 signal 跑 LLM 评分 · 入 candidates 表

    幂等：signal_id 已有 candidate 则返既有的
    """
    existing = (
        await db.execute(
            select(QmTopicCandidate).where(QmTopicCandidate.signal_id == signal.id)
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    post_obj = RedditPost(
        external_id=signal.external_id,
        url=signal.external_url or "",
        title=signal.title or "",
        body=signal.body or "",
        author=signal.author_handle or "?",
        score=signal.score or 0,
        num_comments=signal.num_comments or 0,
        posted_at_ts=int(signal.posted_at.timestamp()) if signal.posted_at else 0,
        top_comments=list(signal.top_comments or []),
    )
    prompt = _build_scoring_prompt(post_obj, source.display_name)

    t0 = time.time()
    parsed, usage = ai_service.complete_json_for(
        "lead_classify",
        prompt,
        system=SCORING_SYSTEM_PROMPT,
        max_tokens=1000,
        temperature=0.2,
    )
    elapsed_ms = int((time.time() - t0) * 1000)

    if not parsed or not isinstance(parsed, dict):
        logger.warning("topic_monitor.score_failed", signal_id=str(signal.id))
        return None

    def _clip(val: Any, lo: int = 0, hi: int = 10) -> int | None:
        try:
            return max(lo, min(hi, int(val)))
        except (TypeError, ValueError):
            return None

    b2b = _clip(parsed.get("b2b_relevance"))
    intent = _clip(parsed.get("search_intent"))
    novelty = _clip(parsed.get("coverage_novelty"))
    match = _clip(parsed.get("factory_match"))

    composite: int | None = None
    if None not in (b2b, intent, novelty, match):
        composite = b2b + intent + novelty + match  # type: ignore

    now = datetime.now(UTC)
    candidate = QmTopicCandidate(
        id=uuid.uuid4(),
        workspace_id=signal.workspace_id,
        signal_id=signal.id,
        b2b_relevance=b2b,
        search_intent=intent,
        coverage_novelty=novelty,
        factory_match=match,
        composite_score=composite,
        distilled_topic=(parsed.get("distilled_topic") or "")[:500] or None,
        distilled_angle=parsed.get("distilled_angle"),
        suggested_title=(parsed.get("suggested_title") or "")[:500] or None,
        suggested_keywords=parsed.get("suggested_keywords") or [],
        target_buyer_persona=parsed.get("target_buyer_persona"),
        status="pending",
        ai_model="lead_classify_chain",
        ai_cost_cny_cents=int(usage.get("cost_cny", 0) * 100),
        ai_processing_time_ms=elapsed_ms,
        created_at=now,
        updated_at=now,
    )
    db.add(candidate)
    await db.flush()
    return candidate


async def score_unscored_signals(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID | None = None,
    limit: int = 200,
) -> dict:
    """跑所有 signals 还没评分的 → 进 candidates"""
    sources_map = {
        s.id: s for s in (await db.execute(select(QmTopicSource))).scalars().all()
    }

    stmt = (
        select(QmTopicSignal)
        .outerjoin(
            QmTopicCandidate, QmTopicCandidate.signal_id == QmTopicSignal.id
        )
        .where(QmTopicCandidate.id.is_(None))
        .order_by(QmTopicSignal.fetched_at.desc())
        .limit(limit)
    )
    if workspace_id:
        stmt = stmt.where(QmTopicSignal.workspace_id == workspace_id)
    signals = (await db.execute(stmt)).scalars().all()

    summary = {"scored": 0, "failed": 0, "above_threshold": 0, "cost_cny_cents": 0}
    for sig in signals:
        src = sources_map.get(sig.source_id)
        if not src:
            continue
        cand = await score_signal(db, sig, src)
        if cand is None:
            summary["failed"] += 1
            continue
        summary["scored"] += 1
        summary["cost_cny_cents"] += cand.ai_cost_cny_cents
        if cand.composite_score and cand.composite_score >= SCORE_THRESHOLD:
            summary["above_threshold"] += 1

    await db.commit()
    return summary


# ─── 查询 ────────────────────────────────────────────────────────────

async def list_top_pending_candidates(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID | None = None,
    top_n: int = 3,
    min_score: int = SCORE_THRESHOLD,
) -> list[QmTopicCandidate]:
    """列 top N pending 候选 · 用于每日推 Sam 微信"""
    stmt = (
        select(QmTopicCandidate)
        .where(
            QmTopicCandidate.status == "pending",
            QmTopicCandidate.composite_score.isnot(None),
            QmTopicCandidate.composite_score >= min_score,
        )
        .order_by(QmTopicCandidate.composite_score.desc())
        .limit(top_n)
    )
    if workspace_id:
        stmt = stmt.where(QmTopicCandidate.workspace_id == workspace_id)
    return list((await db.execute(stmt)).scalars().all())


# ─── Workflow actions ──────────────────────────────────────────────────

async def shortlist_candidate(
    db: AsyncSession,
    *,
    candidate_id: uuid.UUID,
    user_id: uuid.UUID,
) -> QmTopicCandidate:
    """Sam 选定 1 个候选话题 · 进 SEO writer 路径"""
    cand = (
        await db.execute(
            select(QmTopicCandidate).where(QmTopicCandidate.id == candidate_id)
        )
    ).scalar_one_or_none()
    if not cand:
        raise ValueError("candidate not found")
    if cand.status != "pending":
        raise ValueError(f"candidate status={cand.status} · cannot shortlist")

    now = datetime.now(UTC)
    cand.status = "shortlisted"
    cand.shortlisted_at = now
    cand.shortlisted_by_user_id = user_id
    cand.updated_at = now
    await db.commit()
    return cand


async def dismiss_candidate(
    db: AsyncSession,
    *,
    candidate_id: uuid.UUID,
    reason: str,
) -> QmTopicCandidate:
    cand = (
        await db.execute(
            select(QmTopicCandidate).where(QmTopicCandidate.id == candidate_id)
        )
    ).scalar_one_or_none()
    if not cand:
        raise ValueError("candidate not found")
    cand.status = "dismissed"
    cand.dismissed_reason = reason
    cand.updated_at = datetime.now(UTC)
    await db.commit()
    return cand


# ─── 给 SEO writer 用 · 拼上下文 prompt ─────────────────────────────────

def build_seo_writer_context(candidate: QmTopicCandidate, signal: QmTopicSignal) -> str:
    """给 SEO writer 一段 context · 让文章直接基于这个 Reddit 话题写

    返一段可拼到 SEO writer system / user prompt 的文本。
    """
    parts = []
    parts.append("【话题来源】Reddit · 真实买家讨论")
    if signal.external_url:
        parts.append(f"【原帖】{signal.external_url}")
    parts.append(f"【原帖标题】{signal.title or ''}")
    if signal.body:
        parts.append(f"【原帖正文摘要】{signal.body[:800]}")
    if signal.top_comments:
        comments_str = "\n".join(
            f"- {c.get('author', '?')}（{c.get('score', 0)}↑）: {c.get('body', '')[:200]}"
            for c in signal.top_comments[:5]
        )
        parts.append(f"【Top 5 评论】\n{comments_str}")
    if candidate.distilled_topic:
        parts.append(f"【AI 提炼核心命题】{candidate.distilled_topic}")
    if candidate.distilled_angle:
        parts.append(f"【建议写作角度】{candidate.distilled_angle}")
    if candidate.target_buyer_persona:
        parts.append(f"【目标买家画像】{candidate.target_buyer_persona}")
    parts.append(
        "\n要求：基于以上买家真实讨论 · 从中国出口工厂角度回答这个话题 · "
        "加入工厂能力 / 价格区间 / MOQ / 实战经验 · "
        "不是搬运原帖 · 是把 Reddit 上的痛点转化为工厂视角的解决方案文章。"
    )
    return "\n\n".join(parts)
