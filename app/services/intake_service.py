"""Smart Intake v4 · 业务逻辑层

把"工厂 raw folder → DAM 结构化资产"自动化的核心服务。

外层调用顺序：
  1. create_job()        · admin / 小龙 SPA / MCP 创建任务·返 job_id
  2. enqueue_scan()      · 触发 Celery scan 任务
     ↓ Celery chain (tasks_intake.py)
       scan_files → classify_files → cluster_skus → parse_docs → visual_audit (optional) → finalize
  3. status='reviewing'  · 小龙 SPA review queue 1-click approve
  4. enqueue_push()      · 推送到 DAM·写 assets 表
     ↓ Celery
       push_to_dam → status='pushed'

关键设计：
- **路径白名单**：source_path 必须在 INTAKE_ALLOWED_ROOTS 内·防 `/etc/passwd` 攻击
- **dedup by sha256**：同一 job 内不重复·跨 job 也可以查（v4.1）
- **成本预估上前置**：create_job 时立即返 estimated_cost_cny · 用户先看再 confirm
- **审计全程**：created / scanned / classified / clustered / approved / pushed 每个里程碑写 audit_events

测试：tests/intake/test_intake_service.py
"""
from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.deps import Principal
from app.core.logging import get_logger
from app.models.intake import IntakeCluster, IntakeItem, IntakeJob
from app.services import audit_service
from app.services.audit_service import AuditAction
from app.services.intake_prompts import (
    CLASSIFY_CATEGORIES,
    estimate_total_job_cost_cny,
)

logger = get_logger(__name__)


# ════════════════════════════════════════════════════════════
# 路径白名单 · 防越权扫描
# ════════════════════════════════════════════════════════════

def _get_allowed_roots() -> list[str]:
    """从 settings 读 INTAKE_ALLOWED_ROOTS · 默认值兜底"""
    raw = getattr(settings, "INTAKE_ALLOWED_ROOTS", None) or "/mnt/intake,/data/factories"
    if isinstance(raw, str):
        return [p.strip().rstrip("/") for p in raw.split(",") if p.strip()]
    return [str(p).rstrip("/") for p in raw]


def _validate_source_path(source_path: str) -> str:
    """白名单 + 防 ../ 攻击 · 返规范化绝对路径"""
    norm = os.path.realpath(source_path)
    allowed = _get_allowed_roots()
    for root in allowed:
        root_norm = os.path.realpath(root)
        if norm == root_norm or norm.startswith(root_norm + os.sep):
            if not os.path.exists(norm):
                raise ValueError(f"path does not exist: {source_path!r}")
            if not os.path.isdir(norm):
                raise ValueError(f"path is not a directory: {source_path!r}")
            return norm
    raise ValueError(
        f"path not in INTAKE_ALLOWED_ROOTS: {source_path!r} "
        f"(allowed: {', '.join(allowed)})"
    )


# ════════════════════════════════════════════════════════════
# 文件 sha256 + 类型推断（与 v3 assets 一致）
# ════════════════════════════════════════════════════════════

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tiff", ".tif", ".bmp", ".heic"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg"}
DOC_EXTS = {".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".txt", ".md", ".rtf"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz"}


def _kind_from_ext(ext: str) -> str:
    e = ext.lower()
    if e in IMAGE_EXTS:
        return "image"
    if e in VIDEO_EXTS:
        return "video"
    if e in AUDIO_EXTS:
        return "audio"
    if e in DOC_EXTS:
        return "document"
    if e in ARCHIVE_EXTS:
        return "archive"
    return "other"


def _sha256_of_file(path: str, *, buf_size: int = 1024 * 1024) -> str:
    """流式 sha256 · 不一次性读大文件"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(buf_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ════════════════════════════════════════════════════════════
# 1. 创建 intake job
# ════════════════════════════════════════════════════════════

async def create_job(
    db: AsyncSession,
    *,
    principal: Principal,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    factory_slug: str,
    source_path: str,
    options: dict | None = None,
    request=None,
) -> IntakeJob:
    """创建 job · 校验路径 · 估算成本 · 入库

    options:
      - max_files (int) · 限制扫描数·默认 30000
      - skip_visual (bool) · 跳 VL 视觉审核·默认 True（VL 贵 10×）
      - locale (str) · 默认 "zh-CN"
      - dry_run (bool) · 仅扫描 + 分类·不入 DAM
    """
    # 1) 路径白名单
    safe_path = _validate_source_path(source_path)

    # 2) 干跑扫描估算文件数（不算 sha · 只 count）
    file_count, image_count, doc_count = _quick_count(safe_path)

    # 3) 估算成本
    cost_breakdown = estimate_total_job_cost_cny(
        file_count=file_count,
        image_count=image_count,
        doc_count=doc_count,
        skip_visual=(options or {}).get("skip_visual", True),
    )

    # 4) 建 job
    job = IntakeJob(
        tenant_id=tenant_id,
        project_id=project_id,
        factory_slug=factory_slug,
        source_path=safe_path,
        status="scanning",
        created_by_user_id=principal.user_id,
        total_files=file_count,
        options={
            "estimated_cost": cost_breakdown,
            "image_count": image_count,
            "doc_count": doc_count,
            **(options or {}),
        },
    )
    db.add(job)
    await db.flush()

    # 5) 审计
    await audit_service.audit(
        db,
        action=AuditAction.INTAKE_JOB_CREATED,
        tenant_id=tenant_id,
        project_id=project_id,
        actor_user_id=principal.user_id,
        target_kind="intake_job",
        target_id=job.id,
        request=request,
        metadata={
            "factory_slug": factory_slug,
            "source_path": safe_path,
            "file_count": file_count,
            "estimated_cost_cny": cost_breakdown["total_cny"],
        },
    )

    logger.info(
        "intake_job_created",
        job_id=str(job.id),
        factory=factory_slug,
        file_count=file_count,
        est_cost_cny=cost_breakdown["total_cny"],
    )
    return job


def _quick_count(root: str) -> tuple[int, int, int]:
    """O(N) walk · 只数文件不算 sha · 返 (total, image, doc)"""
    total = 0
    images = 0
    docs = 0
    for _dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn.startswith(".") or fn == "Thumbs.db":
                continue
            ext = os.path.splitext(fn)[1].lower()
            kind = _kind_from_ext(ext)
            if kind == "archive":
                # 不展开 · v4.0 不支持 zip 内 walk
                continue
            total += 1
            if kind == "image":
                images += 1
            elif kind == "document":
                docs += 1
    return total, images, docs


# ════════════════════════════════════════════════════════════
# 2. 状态机 + 转换
# ════════════════════════════════════════════════════════════

ALLOWED_INTAKE_TRANSITIONS = {
    "scanning": {"classifying", "failed", "cancelled"},
    "classifying": {"clustering", "failed", "cancelled"},
    "clustering": {"parsing_docs", "failed", "cancelled"},
    "parsing_docs": {"visual_audit", "finalizing", "failed", "cancelled"},
    "visual_audit": {"finalizing", "failed", "cancelled"},
    "finalizing": {"reviewing", "failed", "cancelled"},
    "reviewing": {"approved", "rejected", "cancelled"},
    "approved": {"pushing", "cancelled"},
    "pushing": {"pushed", "failed"},
    "pushed": set(),
    "rejected": set(),
    "failed": set(),
    "cancelled": set(),
}


async def transition_status(
    db: AsyncSession,
    *,
    job: IntakeJob,
    new_status: str,
    principal: Principal | None = None,
    reason: str | None = None,
    request=None,
) -> IntakeJob:
    """合法状态机切换 · 写时间戳 · 写 audit"""
    if new_status not in ALLOWED_INTAKE_TRANSITIONS.get(job.status, set()):
        raise ValueError(
            f"invalid transition: {job.status} → {new_status}"
        )

    old_status = job.status
    job.status = new_status
    job.updated_at = datetime.now(timezone.utc)

    # 时间戳
    now = datetime.now(timezone.utc)
    if new_status == "classifying":
        job.scan_completed_at = now
    elif new_status == "reviewing":
        job.review_at = now
    elif new_status == "approved":
        job.approved_at = now
        if principal:
            job.approved_by_user_id = principal.user_id
    elif new_status in ("pushed", "rejected", "failed", "cancelled"):
        job.completed_at = now
        if reason and new_status in ("failed", "rejected"):
            job.failed_reason = reason

    await db.flush()

    # audit
    action_map = {
        "classifying": AuditAction.INTAKE_JOB_SCANNED,
        "clustering": AuditAction.INTAKE_JOB_CLASSIFIED,
        "parsing_docs": AuditAction.INTAKE_JOB_CLUSTERED,
        "reviewing": AuditAction.INTAKE_JOB_REVIEW_READY,
        "approved": AuditAction.INTAKE_JOB_APPROVED,
        "rejected": AuditAction.INTAKE_JOB_REJECTED,
        "pushed": AuditAction.INTAKE_JOB_PUSHED,
        "failed": AuditAction.INTAKE_JOB_FAILED,
    }
    action = action_map.get(new_status)
    if action and principal:
        await audit_service.audit(
            db,
            action=action,
            tenant_id=job.tenant_id,
            project_id=job.project_id,
            actor_user_id=principal.user_id,
            target_kind="intake_job",
            target_id=job.id,
            request=request,
            metadata={
                "from": old_status,
                "to": new_status,
                "reason": reason,
            },
        )

    logger.info(
        "intake_job_transition",
        job_id=str(job.id),
        from_status=old_status,
        to_status=new_status,
    )
    return job


# ════════════════════════════════════════════════════════════
# 3. 列表 / 详情查询
# ════════════════════════════════════════════════════════════

async def list_jobs(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
    factory_slug: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[IntakeJob]:
    """分页列出 jobs · 按 created_at desc"""
    q = select(IntakeJob).where(IntakeJob.tenant_id == tenant_id)
    if project_id is not None:
        q = q.where(IntakeJob.project_id == project_id)
    if factory_slug:
        q = q.where(IntakeJob.factory_slug == factory_slug)
    if status:
        q = q.where(IntakeJob.status == status)
    q = q.order_by(IntakeJob.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_job(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    eager_clusters: bool = False,
) -> IntakeJob | None:
    q = select(IntakeJob).where(
        IntakeJob.id == job_id,
        IntakeJob.tenant_id == tenant_id,
    )
    if eager_clusters:
        q = q.options(selectinload(IntakeJob.clusters))
    result = await db.execute(q)
    return result.scalar_one_or_none()


async def list_items(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    category: str | None = None,
    sku_slug: str | None = None,
    flagged_only: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> list[IntakeItem]:
    """job 下的文件清单"""
    q = select(IntakeItem).where(IntakeItem.job_id == job_id)
    if category:
        q = q.where(IntakeItem.predicted_category == category)
    if sku_slug:
        q = q.where(IntakeItem.predicted_sku_slug == sku_slug)
    if flagged_only:
        q = q.where(IntakeItem.flagged_reason.is_not(None))
    q = q.order_by(IntakeItem.predicted_sku_slug.asc().nulls_last(),
                   IntakeItem.predicted_category.asc()).limit(limit).offset(offset)
    result = await db.execute(q)
    return list(result.scalars().all())


# ════════════════════════════════════════════════════════════
# 4. 用户决策（approve / reject / override）
# ════════════════════════════════════════════════════════════

async def approve_item(
    db: AsyncSession,
    *,
    principal: Principal,
    item: IntakeItem,
    override: dict | None = None,
    request=None,
) -> IntakeItem:
    """逐文件 approve · override 可改 subdir / filename / tags"""
    item.user_decision = "approve" if not override else "edit"
    item.user_override = override
    item.user_decision_at = datetime.now(timezone.utc)
    if override:
        # 把 override 字段同步到 predicted_* 列·便于 push 时统一读
        if "predicted_category" in override:
            item.predicted_category = override["predicted_category"]
        if "predicted_sku_slug" in override:
            item.predicted_sku_slug = override["predicted_sku_slug"]
        if "predicted_subdir" in override:
            item.predicted_subdir = override["predicted_subdir"]
        if "predicted_target_filename" in override:
            item.predicted_target_filename = override["predicted_target_filename"]
        if "predicted_tags" in override:
            item.predicted_tags = override["predicted_tags"]
        # 审计 override（这是 BD 改动算法结果·关键证据）
        await audit_service.audit(
            db,
            action=AuditAction.INTAKE_ITEM_OVERRIDDEN,
            tenant_id=item.job.tenant_id,
            project_id=item.job.project_id,
            actor_user_id=principal.user_id,
            target_kind="intake_item",
            target_id=item.id,
            request=request,
            metadata={"override": override},
        )
    await db.flush()
    return item


async def reject_item(
    db: AsyncSession,
    *,
    item: IntakeItem,
    reason: str | None = None,
) -> IntakeItem:
    item.user_decision = "reject"
    item.user_decision_at = datetime.now(timezone.utc)
    if reason:
        item.user_override = {"reject_reason": reason}
    await db.flush()
    return item


async def bulk_decide(
    db: AsyncSession,
    *,
    principal: Principal,
    job_id: uuid.UUID,
    item_ids: list[uuid.UUID],
    decision: str,  # "approve" | "reject"
    request=None,
) -> int:
    """批量决策 · 1-click "全部 approve"·返影响行数"""
    if decision not in ("approve", "reject"):
        raise ValueError(f"invalid decision: {decision!r}")

    stmt = (
        update(IntakeItem)
        .where(
            IntakeItem.job_id == job_id,
            IntakeItem.id.in_(item_ids),
        )
        .values(
            user_decision=decision,
            user_decision_at=datetime.now(timezone.utc),
        )
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.rowcount or 0


# ════════════════════════════════════════════════════════════
# 5. cluster 用户改名（BD 改 SKU slug）
# ════════════════════════════════════════════════════════════

async def rename_cluster(
    db: AsyncSession,
    *,
    principal: Principal,
    cluster: IntakeCluster,
    new_slug: str,
    request=None,
) -> IntakeCluster:
    """BD 改 cluster slug · 影响所有归属此 cluster 的 items 的 push 路径"""
    old_slug = cluster.sku_slug
    cluster.user_renamed_slug = new_slug
    cluster.user_confirmed = True

    # 把所有 items 的 predicted_sku_slug 同步更新
    stmt = (
        update(IntakeItem)
        .where(
            IntakeItem.cluster_id == cluster.id,
            IntakeItem.predicted_sku_slug == old_slug,
        )
        .values(predicted_sku_slug=new_slug)
    )
    await db.execute(stmt)
    await db.flush()

    await audit_service.audit(
        db,
        action=AuditAction.INTAKE_CLUSTER_RENAMED,
        tenant_id=cluster.job.tenant_id,
        project_id=cluster.job.project_id,
        actor_user_id=principal.user_id,
        target_kind="intake_cluster",
        target_id=cluster.id,
        request=request,
        metadata={"from": old_slug, "to": new_slug},
    )
    return cluster


# ════════════════════════════════════════════════════════════
# 6. 成本累计 helper（Celery task 调）
# ════════════════════════════════════════════════════════════

async def bump_job_cost(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    input_tokens: int,
    output_tokens: int,
    cost_cny: float,
) -> None:
    """累计 LLM 成本到 job · 原子 UPDATE"""
    stmt = (
        update(IntakeJob)
        .where(IntakeJob.id == job_id)
        .values(
            llm_tokens_input=IntakeJob.llm_tokens_input + input_tokens,
            llm_tokens_output=IntakeJob.llm_tokens_output + output_tokens,
            llm_cost_cny=IntakeJob.llm_cost_cny + cost_cny,
        )
    )
    await db.execute(stmt)


# ════════════════════════════════════════════════════════════
# 7. 统计·给 SPA dashboard
# ════════════════════════════════════════════════════════════

async def job_summary(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
) -> dict:
    """job 仪表盘 · 分类 / cluster / 决策 / 成本"""
    # 按 category 聚合
    cat_q = (
        select(
            IntakeItem.predicted_category,
            func.count(IntakeItem.id).label("count"),
        )
        .where(IntakeItem.job_id == job_id)
        .group_by(IntakeItem.predicted_category)
    )
    cat_result = await db.execute(cat_q)
    by_category = {row[0] or "uncategorized": row[1] for row in cat_result.all()}

    # 按 decision 聚合
    dec_q = (
        select(
            IntakeItem.user_decision,
            func.count(IntakeItem.id).label("count"),
        )
        .where(IntakeItem.job_id == job_id)
        .group_by(IntakeItem.user_decision)
    )
    dec_result = await db.execute(dec_q)
    by_decision = {row[0] or "pending": row[1] for row in dec_result.all()}

    # cluster 数
    cluster_count_q = select(func.count(IntakeCluster.id)).where(
        IntakeCluster.job_id == job_id
    )
    cluster_count = (await db.execute(cluster_count_q)).scalar_one() or 0

    # flagged
    flagged_q = select(func.count(IntakeItem.id)).where(
        IntakeItem.job_id == job_id,
        IntakeItem.flagged_reason.is_not(None),
    )
    flagged_count = (await db.execute(flagged_q)).scalar_one() or 0

    return {
        "by_category": by_category,
        "by_decision": by_decision,
        "cluster_count": cluster_count,
        "flagged_count": flagged_count,
    }


__all__ = [
    "ALLOWED_INTAKE_TRANSITIONS",
    "CLASSIFY_CATEGORIES",
    "approve_item",
    "bulk_decide",
    "bump_job_cost",
    "create_job",
    "get_job",
    "job_summary",
    "list_items",
    "list_jobs",
    "reject_item",
    "rename_cluster",
    "transition_status",
]
