"""ai_enrichment_service · 用 LLM 给 lead 增加 AI 元数据

3 个能力：
  1. ai_intent_summary  · 1-2 句话总结买家意图
  2. ai_translated_zh   · 中文翻译（如询盘非中文）
  3. ai_suggested_reply · 起草回复初稿（BD 改后发）

策略：
  - Celery 任务·新 lead 创建后异步跑（不阻塞 API）
  - 失败不 raise · log + 跳过（与 ai_service stub 模式一致）
  - 用 DashScope qwen-plus / qwen-turbo · 中文极强 + 便宜
  - 成本：每 lead ~1500 tokens × ¥4/1M = ¥0.006 / lead · 100 工厂日 1000 leads = ¥6/月
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.models.crm.lead import Lead
from app.services import ai_service

logger = get_logger(__name__)


async def enrich_lead(db, lead_id: uuid.UUID) -> None:
    """异步给 lead 加 AI 元数据·失败不 raise"""
    if not settings.DASHSCOPE_API_KEY:
        logger.info("ai_enrichment.skip", reason="no_dashscope_key", lead_id=str(lead_id))
        return

    lead = await db.get(Lead, lead_id)
    if not lead:
        return

    # 跳过已有完整 AI 元数据的（重跑 idempotent）
    if lead.ai_intent_summary and lead.ai_suggested_reply:
        return

    try:
        result = await _call_dashscope_enrich(
            inquiry_text=lead.inquiry_text,
            contact_name=lead.contact_name,
            contact_company=lead.contact_company,
            contact_role=lead.contact_role,
            factory_slug=lead.factory_slug,
            language_hint=lead.inquiry_language,
        )
        if not result:
            return

        if result.get("intent_summary"):
            lead.ai_intent_summary = result["intent_summary"][:500]
        if result.get("translated_zh"):
            lead.ai_translated_zh = result["translated_zh"][:2000]
        if result.get("suggested_reply"):
            lead.ai_suggested_reply = result["suggested_reply"][:2000]
        if result.get("competitors_mentioned"):
            lead.ai_competitors_mentioned = [
                str(c)[:128] for c in result["competitors_mentioned"][:5]
            ]
        if result.get("urgency_score") is not None:
            lead.ai_urgency_score = float(result["urgency_score"])
        if result.get("quality_score") is not None:
            lead.ai_quality_score = float(result["quality_score"])

        lead.ai_model = "qwen-plus@dashscope"
        await db.flush()
        logger.info("ai_enrichment.done", lead_id=str(lead_id))

    except Exception as e:  # noqa: BLE001
        logger.warning("ai_enrichment.failed", lead_id=str(lead_id), error=str(e)[:200])


async def _call_dashscope_enrich(
    inquiry_text: str,
    contact_name: Optional[str],
    contact_company: Optional[str],
    contact_role: Optional[str],
    factory_slug: str,
    language_hint: Optional[str],
) -> Optional[dict]:
    """单次 LLM 调用·拿 4-6 个 AI 字段"""
    prompt = f"""你是 B2B 外贸 BD 助手。请分析以下询盘 · 输出 JSON。

【询盘原文】
{inquiry_text[:3000]}

【联系人信息】
姓名：{contact_name or "未知"}
公司：{contact_company or "未知"}
职位：{contact_role or "未知"}
工厂：{factory_slug}

输出 JSON（严格按 schema · 不输出其它文字）：
{{
  "intent_summary": "<50 字内·1-2 句话总结买家想要什么>",
  "translated_zh": "<如原文是中文则空字符串 · 否则翻译成中文>",
  "suggested_reply": "<200-400 字英文回复初稿·热情专业·必含：致谢 + 确认理解 + 关键问题（数量/规格/时限）+ 行动号召>",
  "competitors_mentioned": ["如询盘里提到的竞品品牌·如无则空数组"],
  "urgency_score": <0.0-1.0·询盘的急迫度>,
  "quality_score": <0.0-1.0·与该工厂业务的匹配度>
}}
"""

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                ai_service.DASHSCOPE_TEXT_GEN_URL,
                json={
                    "model": "qwen-plus",
                    "input": {"messages": [{"role": "user", "content": prompt}]},
                    "parameters": {
                        "result_format": "message",
                        "max_tokens": 1500,
                        "temperature": 0.3,
                    },
                },
                headers={"Authorization": f"Bearer {settings.DASHSCOPE_API_KEY}"},
            )
            resp.raise_for_status()
            data = resp.json()
            content = (
                data.get("output", {}).get("choices", [{}])[0]
                    .get("message", {}).get("content", "")
            )
            # 提取 JSON
            content = content.strip()
            if content.startswith("```"):
                content = content.split("```")[1].lstrip("json").strip()
            return json.loads(content)
    except Exception as e:  # noqa: BLE001
        logger.warning("dashscope.enrich_call_failed", error=str(e)[:200])
        return None


# ════════════════════════════════════════════════════════════
# Celery 任务包装·让 leads_service 异步触发
# ════════════════════════════════════════════════════════════

async def enrich_lead_async_dispatch(lead_id: uuid.UUID) -> None:
    """leads_service.create_lead 末尾调用·dispatch 异步任务

    生产实现：
      from app.workers.celery_app import celery_app
      celery_app.send_task("crm.enrich_lead", args=[str(lead_id)])

    Dev / 当前 v0.3：同步内联跑（< 2 秒 · 不阻塞太久）
    """
    # TODO v7.1：真接 Celery
    # 当前作 stub · log 一下
    logger.info("ai_enrichment.dispatched_inline", lead_id=str(lead_id))
