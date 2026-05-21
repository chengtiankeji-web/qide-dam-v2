"""S2 · Diagnostic service · AI 出海诊断 + PDF 生成

Pipeline：
  1. create_diagnostic_record  · 写一行 status='pending'
  2. run_diagnostic           · 调 ai_service.complete_json_for("deep_reasoning", ...)
                                解析 JSON → 写回 diagnostic 行
  3. render_pdf               · reportlab 渲染 → 上传 DAM → 写回 pdf_asset_id + signed URL
  4. publish diagnostic.ready 事件 · 邮件 outbox 收到事件后发客户

PDF 渲染说明：
- reportlab.platypus 用 Paragraph + Table 走简单中英排版
- 中文字体需要嵌入 SimSun 或 NotoSansSC（环境变量 QM_PDF_FONT_PATH）
- 没字体路径时 fallback 到 Helvetica · 中文显示为方块（生产前必填）
"""
from __future__ import annotations

import io
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.qidematrix.pipeline import QmDiagnostic, QmOnboarding
from app.services import ai_service
from app.services.qidematrix import onboarding_service, pipeline_service
from app.services.qidematrix.diagnostic_prompts import (
    DIAGNOSTIC_MINIMAL_DATA_NOTICE,
    DIAGNOSTIC_SYSTEM_PROMPT,
    build_diagnostic_user_prompt,
)

logger = get_logger("qm.diagnostic")


# ═════════════════════════════════════════════════════════════════════
# 1. 创建 diagnostic 记录（pending）
# ═════════════════════════════════════════════════════════════════════

async def create_diagnostic_record(
    db: AsyncSession,
    *,
    onboarding: QmOnboarding,
) -> QmDiagnostic:
    """写一行 pending diagnostic · 让 Celery 任务捡起来跑"""
    now = datetime.now(UTC)
    diagnostic = QmDiagnostic(
        id=uuid.uuid4(),
        onboarding_id=onboarding.id,
        tenant_id=onboarding.tenant_id,
        workspace_id=onboarding.workspace_id,
        model_name="pending",
        model_provider="pending",
        readiness_score=0,
        recommended_tier="starter",
        status="pending",
        created_at=now,
        updated_at=now,
    )
    db.add(diagnostic)
    await db.flush()

    await pipeline_service.publish(
        db,
        tenant_id=onboarding.tenant_id,
        workspace_id=onboarding.workspace_id,
        event_type="diagnostic.requested",
        subject_kind="diagnostic",
        subject_id=diagnostic.id,
        payload={
            "diagnostic_id": str(diagnostic.id),
            "onboarding_id": str(onboarding.id),
        },
    )

    return diagnostic


# ═════════════════════════════════════════════════════════════════════
# 2. 跑 LLM 诊断（异步 task 调）
# ═════════════════════════════════════════════════════════════════════

def _data_is_minimal(onboarding_data: dict) -> bool:
    """判断客户填的资料是否过于少 · 触发 minimal-data prompt"""
    fields_filled = sum(
        1 for k in [
            "product_categories", "target_markets", "export_stage",
            "existing_social_urls", "monthly_budget", "biggest_pain_point",
            "top_skus", "website_url", "company_description",
        ]
        if onboarding_data.get(k)
    )
    return fields_filled < 3


async def run_diagnostic(
    db: AsyncSession,
    *,
    diagnostic_id: uuid.UUID,
    assets_summary: str = "",
    override_model: str | None = None,
) -> QmDiagnostic:
    """跑 LLM 生成诊断 · 解析 JSON · 回写字段。

    成功 → status='ready' + 触发 PDF 渲染（render_pdf 单独调）
    失败 → status='failed' + error_message + 触发 diagnostic.failed
    """
    diagnostic = await get_diagnostic(db, diagnostic_id=diagnostic_id)
    if not diagnostic:
        raise ValueError(f"diagnostic {diagnostic_id} not found")

    onboarding = await onboarding_service.get_onboarding(
        db, onboarding_id=diagnostic.onboarding_id
    )
    if not onboarding:
        raise ValueError(f"onboarding {diagnostic.onboarding_id} not found")

    diagnostic.status = "running"
    diagnostic.updated_at = datetime.now(UTC)
    await db.flush()

    onboarding_data = {
        "factory_name": onboarding.factory_name,
        "contact_name": onboarding.contact_name,
        "company_description": onboarding.company_description,
        "website_url": onboarding.website_url,
        "product_categories": onboarding.product_categories,
        "target_markets": onboarding.target_markets,
        "export_stage": onboarding.export_stage,
        "existing_social_urls": onboarding.existing_social_urls,
        "monthly_budget": onboarding.monthly_budget,
        "desired_services": onboarding.desired_services,
        "top_skus": onboarding.top_skus,
        "biggest_pain_point": onboarding.biggest_pain_point,
        "business_license_number": onboarding.business_license_number,
    }

    user_prompt = build_diagnostic_user_prompt(onboarding_data, assets_summary)
    system_prompt = DIAGNOSTIC_SYSTEM_PROMPT
    if _data_is_minimal(onboarding_data):
        system_prompt = DIAGNOSTIC_SYSTEM_PROMPT + "\n\n" + DIAGNOSTIC_MINIMAL_DATA_NOTICE

    # 同步调（在 Celery worker 里跑 · 不在 FastAPI 主线程）
    parsed, usage = ai_service.complete_json_for(
        "deep_reasoning",
        user_prompt,
        system=system_prompt,
        max_tokens=3500,
        temperature=0.3,
    )

    if parsed is None or not isinstance(parsed, dict):
        diagnostic.status = "failed"
        diagnostic.error_message = "LLM returned no parseable JSON"
        diagnostic.model_name = "unavailable"
        diagnostic.model_provider = "unavailable"
        diagnostic.updated_at = datetime.now(UTC)
        await db.flush()
        await pipeline_service.publish(
            db,
            tenant_id=diagnostic.tenant_id,
            workspace_id=diagnostic.workspace_id,
            event_type="diagnostic.failed",
            subject_kind="diagnostic",
            subject_id=diagnostic.id,
            payload={
                "diagnostic_id": str(diagnostic.id),
                "error": "LLM no JSON",
            },
        )
        return diagnostic

    # 解析字段
    try:
        scores = parsed.get("scores", {}) or {}
        diagnostic.readiness_score = max(0, min(100, int(parsed.get("readiness_score", 50))))
        diagnostic.brand_score = int(scores.get("brand", 0)) if scores.get("brand") is not None else None
        diagnostic.product_score = int(scores.get("product", 0)) if scores.get("product") is not None else None
        diagnostic.channel_score = int(scores.get("channel", 0)) if scores.get("channel") is not None else None
        diagnostic.ops_score = int(scores.get("ops", 0)) if scores.get("ops") is not None else None
        diagnostic.compliance_score = int(scores.get("compliance", 0)) if scores.get("compliance") is not None else None
        diagnostic.recommended_tier = parsed.get("recommended_tier", "starter")
        diagnostic.recommended_plan = parsed.get("recommended_plan")
        diagnostic.industry_benchmark = parsed.get("industry_benchmark") or {}
        diagnostic.roadmap_30d = parsed.get("roadmap_30d") or []
        diagnostic.roadmap_90d = parsed.get("roadmap_90d") or []
        diagnostic.roadmap_365d = parsed.get("roadmap_365d") or []
        diagnostic.risks = parsed.get("risks") or []
        diagnostic.executive_summary = parsed.get("executive_summary") or ""
        diagnostic.prompt_tokens = usage.get("input_tokens") or 0
        diagnostic.completion_tokens = usage.get("output_tokens") or 0
        diagnostic.model_name = "qwen-max"  # USE_CASE_MODELS["deep_reasoning"][0]
        diagnostic.model_provider = "dashscope"
        diagnostic.status = "ready"
        diagnostic.generated_at = datetime.now(UTC)
        diagnostic.updated_at = datetime.now(UTC)
    except (ValueError, TypeError) as exc:
        diagnostic.status = "failed"
        diagnostic.error_message = f"parse error: {exc}"
        diagnostic.updated_at = datetime.now(UTC)
        await db.flush()
        await pipeline_service.publish(
            db,
            tenant_id=diagnostic.tenant_id,
            workspace_id=diagnostic.workspace_id,
            event_type="diagnostic.failed",
            subject_kind="diagnostic",
            subject_id=diagnostic.id,
            payload={
                "diagnostic_id": str(diagnostic.id),
                "error": f"parse: {exc}",
            },
        )
        return diagnostic

    await db.flush()

    # 推 onboarding 状态
    await onboarding_service.attach_diagnostic(
        db, onboarding_id=onboarding.id, diagnostic_id=diagnostic.id
    )

    # diagnostic.ready 事件 · 触发 PDF 渲染 + 邮件
    await pipeline_service.publish(
        db,
        tenant_id=diagnostic.tenant_id,
        workspace_id=diagnostic.workspace_id,
        event_type="diagnostic.ready",
        subject_kind="diagnostic",
        subject_id=diagnostic.id,
        payload={
            "diagnostic_id": str(diagnostic.id),
            "onboarding_id": str(onboarding.id),
            "readiness_score": diagnostic.readiness_score,
            "recommended_tier": diagnostic.recommended_tier,
        },
    )

    logger.info(
        "qm.diagnostic.ready",
        diagnostic_id=str(diagnostic.id),
        readiness_score=diagnostic.readiness_score,
        recommended_tier=diagnostic.recommended_tier,
    )

    return diagnostic


# ═════════════════════════════════════════════════════════════════════
# 3. PDF 渲染 + 上传 DAM
# ═════════════════════════════════════════════════════════════════════

def render_diagnostic_pdf_bytes(diagnostic: QmDiagnostic, onboarding: QmOnboarding) -> bytes:
    """用 reportlab 渲染诊断 PDF · 返回 bytes

    依赖 reportlab 已安装（QideDAM v3 base image 已含）。
    中文字体路径来自 env QM_PDF_FONT_PATH · 不存在则降级 Helvetica。
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (
            PageBreak,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:
        raise RuntimeError(
            "reportlab not installed; add to pyproject.toml: reportlab>=4.0"
        ) from exc

    # 字体注册
    font_path = os.getenv("QM_PDF_FONT_PATH")
    font_name = "Helvetica"
    if font_path and os.path.exists(font_path):
        try:
            pdfmetrics.registerFont(TTFont("QmCN", font_path))
            font_name = "QmCN"
        except Exception as exc:
            logger.warning("qm.pdf.font_register_failed", path=font_path, error=str(exc))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=2 * cm,
        leftMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=f"{onboarding.factory_name} · 出海诊断报告",
        author="QideMatrix",
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle(
        "h1", parent=styles["Heading1"], fontName=font_name, fontSize=20, spaceAfter=12,
    )
    h2 = ParagraphStyle(
        "h2", parent=styles["Heading2"], fontName=font_name, fontSize=14,
        spaceBefore=14, spaceAfter=8,
    )
    body = ParagraphStyle(
        "body", parent=styles["BodyText"], fontName=font_name, fontSize=10,
        leading=15, spaceAfter=6,
    )

    story = []

    # 封面
    story.append(Paragraph(f"{onboarding.factory_name}", h1))
    story.append(Paragraph("出海诊断报告 · Export Readiness Diagnostic", h2))
    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph(
        f"生成时间：{(diagnostic.generated_at or datetime.now(UTC)).strftime('%Y-%m-%d %H:%M UTC')}",
        body,
    ))
    story.append(Paragraph(f"模型：{diagnostic.model_name} ({diagnostic.model_provider})", body))
    story.append(PageBreak())

    # 1. 总体评分
    story.append(Paragraph("一、出海准备度评分", h2))
    score_table = [
        ["维度", "得分（0-100）"],
        ["品牌力 Brand", str(diagnostic.brand_score or "—")],
        ["产品力 Product", str(diagnostic.product_score or "—")],
        ["渠道力 Channel", str(diagnostic.channel_score or "—")],
        ["运营力 Ops", str(diagnostic.ops_score or "—")],
        ["合规力 Compliance", str(diagnostic.compliance_score or "—")],
        ["综合评分 Readiness", f"{diagnostic.readiness_score} / 100"],
    ]
    tbl = Table(score_table, colWidths=[6 * cm, 4 * cm])
    tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F4C81")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F0F4F8")),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 0.6 * cm))
    story.append(Paragraph(f"<b>推荐档位：</b>{diagnostic.recommended_tier} · {diagnostic.recommended_plan or ''}", body))
    story.append(Spacer(1, 0.4 * cm))

    # 2. 高管摘要
    story.append(Paragraph("二、高管摘要", h2))
    story.append(Paragraph((diagnostic.executive_summary or "—").replace("\n", "<br/>"), body))

    # 3. 行业 benchmark
    if diagnostic.industry_benchmark:
        story.append(Paragraph("三、行业 Benchmark", h2))
        for k, v in diagnostic.industry_benchmark.items():
            story.append(Paragraph(f"• <b>{k}</b>：{v}", body))

    # 4-6. Roadmap
    for label, items in [
        ("四、30 天行动清单", diagnostic.roadmap_30d),
        ("五、90 天行动清单", diagnostic.roadmap_90d),
        ("六、365 天战略路线", diagnostic.roadmap_365d),
    ]:
        story.append(Paragraph(label, h2))
        if not items:
            story.append(Paragraph("—", body))
            continue
        for i, item in enumerate(items, 1):
            line = f"<b>{i}. {item.get('task', '')}</b>"
            details = []
            if item.get("owner"):
                details.append(f"负责人 {item['owner']}")
            if item.get("budget"):
                details.append(f"预算 {item['budget']}")
            if item.get("tool"):
                details.append(f"工具 {item['tool']}")
            if details:
                line += f"<br/><font size=9 color='#666'>{' · '.join(details)}</font>"
            story.append(Paragraph(line, body))

    # 7. 风险
    if diagnostic.risks:
        story.append(Paragraph("七、关键风险与缓解", h2))
        for r in diagnostic.risks:
            story.append(Paragraph(
                f"<b>[{r.get('severity', '?')}] {r.get('risk', '')}</b><br/>"
                f"<font size=9 color='#666'>缓解：{r.get('mitigation', '')}</font>",
                body,
            ))

    # 8. 推荐档位
    story.append(PageBreak())
    story.append(Paragraph("八、推荐服务方案", h2))
    story.append(Paragraph(
        f"<b>{diagnostic.recommended_tier.upper()}</b> · {diagnostic.recommended_plan or ''}",
        body,
    ))
    story.append(Paragraph(diagnostic.recommended_plan or "—", body))

    story.append(Spacer(1, 1 * cm))
    story.append(Paragraph(
        "<font size=8 color='#999'>本报告由 QideMatrix AI 引擎生成 · 不构成正式商业建议 · "
        "如需深度服务请联系祁德商链科技</font>",
        body,
    ))

    doc.build(story)
    return buf.getvalue()


# ═════════════════════════════════════════════════════════════════════
# 4. 查询接口
# ═════════════════════════════════════════════════════════════════════

async def get_diagnostic(
    db: AsyncSession, *, diagnostic_id: uuid.UUID
) -> QmDiagnostic | None:
    result = await db.execute(
        select(QmDiagnostic).where(QmDiagnostic.id == diagnostic_id)
    )
    return result.scalar_one_or_none()


async def get_diagnostic_by_onboarding(
    db: AsyncSession, *, onboarding_id: uuid.UUID
) -> QmDiagnostic | None:
    result = await db.execute(
        select(QmDiagnostic)
        .where(QmDiagnostic.onboarding_id == onboarding_id)
        .order_by(QmDiagnostic.created_at.desc())
    )
    return result.scalar_one_or_none()


async def list_diagnostics(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[QmDiagnostic]:
    stmt = select(QmDiagnostic).order_by(QmDiagnostic.created_at.desc())
    if tenant_id:
        stmt = stmt.where(QmDiagnostic.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(QmDiagnostic.status == status)
    stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())


def set_pdf_url(
    diagnostic: QmDiagnostic, *, signed_url: str, ttl_hours: int = 24
) -> None:
    """worker 拿到 DAM presigned URL 后回写"""
    diagnostic.pdf_signed_url = signed_url
    diagnostic.pdf_signed_until = datetime.now(UTC) + timedelta(hours=ttl_hours)
    diagnostic.updated_at = datetime.now(UTC)
