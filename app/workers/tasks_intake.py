"""Smart Intake v4 · Celery 任务链

Pipeline（intake.run_pipeline 入口）：
  scan_files → classify_files → cluster_skus → parse_docs → [visual_audit?] → finalize
                                                                        ↓
                                                                 status='reviewing'
  ← user review (admin SPA / MCP) →

  approve → push_to_dam → status='pushed'

设计：
1. **immutable signatures** (.si())·链中任一 task 失败不会阻塞 finalize
2. **session_scope** 同步 DB · Celery 不用 asyncpg
3. **空文件夹兜底**：scan 完发现 0 文件 → status='failed' + 不入队后续
4. **LLM 不可用兜底**：classify 阶段无 DASHSCOPE_API_KEY → rule-only · confidence 整体偏低
5. **路径白名单**：scan 用 intake_service._validate_source_path 再校验一次（防 job 表被改）
6. **审计**：每个 status 变更走 audit_service · 在 service 层落地

Sprint 4.0 暂不实装：
- 真正调 LLM（先用 rule-only 占位 · Block 2.5 接 LLM）
- visual_audit Qwen-VL 调用（占位）
- push_to_dam 真把文件传 R2（占位 · 写 LOG 即可·真实装在 Block 4）
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import uuid
from datetime import datetime, timezone

from celery import chain
from sqlalchemy import select

from app.core.logging import get_logger
from app.models.intake import IntakeCluster, IntakeItem, IntakeJob
from app.services.intake_prompts import CLASSIFY_CATEGORIES
from app.workers._db import session_scope
from app.workers.celery_app import celery_app

logger = get_logger("worker.intake")


# ════════════════════════════════════════════════════════════
# 工具：文件类型 + sha + 安全文件名
# ════════════════════════════════════════════════════════════

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tiff", ".tif", ".bmp", ".heic"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg"}
DOC_EXTS = {".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".txt", ".md", ".rtf"}


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
    return "other"


def _sha256_of_file(path: str, *, buf_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(buf_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _safe_slug(text: str) -> str:
    """简易 slug · 中文转拼音留待 v4.1·此处用 fallback"""
    t = re.sub(r"[^a-zA-Z0-9-]+", "-", text.lower())
    t = re.sub(r"-+", "-", t).strip("-")
    return t[:128] or "unnamed"


# ════════════════════════════════════════════════════════════
# 1. scan_files · 遍历 source_path · 入 intake_items
# ════════════════════════════════════════════════════════════

@celery_app.task(name="intake.scan_files", bind=True, queue="default")
def scan_files(self, job_id: str) -> dict:
    """walk 文件夹 · 每文件入 intake_items · 算 sha256"""
    job_uuid = uuid.UUID(job_id)
    with session_scope() as db:
        job = db.get(IntakeJob, job_uuid)
        if not job:
            return {"job_id": job_id, "status": "missing"}
        root = job.source_path

        # 安全：白名单二次校验（防 job 表被改）
        from app.services.intake_service import _get_allowed_roots
        allowed = _get_allowed_roots()
        norm = os.path.realpath(root)
        if not any(
            norm == os.path.realpath(r) or norm.startswith(os.path.realpath(r) + os.sep)
            for r in allowed
        ):
            job.status = "failed"
            job.failed_reason = f"source_path not in INTAKE_ALLOWED_ROOTS: {root!r}"
            job.completed_at = datetime.now(timezone.utc)
            db.add(job)
            return {"job_id": job_id, "status": "failed", "reason": "path_not_allowed"}

        seen_sha: set[str] = set()
        dup_count = 0
        total = 0

        for dirpath, _, filenames in os.walk(norm):
            for fn in filenames:
                if fn.startswith(".") or fn == "Thumbs.db":
                    continue
                abs_path = os.path.join(dirpath, fn)
                try:
                    stat = os.stat(abs_path)
                except OSError as exc:
                    logger.warning("scan_stat_failed", path=abs_path, error=str(exc))
                    continue

                ext = os.path.splitext(fn)[1].lower()
                kind = _kind_from_ext(ext)
                mime = mimetypes.guess_type(fn)[0]

                try:
                    sha = _sha256_of_file(abs_path)
                except OSError as exc:
                    logger.warning("scan_sha_failed", path=abs_path, error=str(exc))
                    continue

                if sha in seen_sha:
                    dup_count += 1
                    continue
                seen_sha.add(sha)

                item = IntakeItem(
                    job_id=job_uuid,
                    source_path=abs_path,
                    filename=fn,
                    size_bytes=stat.st_size,
                    sha256=sha,
                    mime_type=mime,
                    kind=kind,
                )
                db.add(item)
                total += 1
                if total % 200 == 0:
                    db.flush()

        # 写回 job 统计
        job.total_files = total
        job.duplicate_count = dup_count
        job.status = "classifying"
        job.scan_completed_at = datetime.now(timezone.utc)
        db.add(job)

        logger.info(
            "intake.scan.done", job_id=job_id, total=total, duplicates=dup_count,
        )
        return {"job_id": job_id, "total_files": total, "duplicates": dup_count}


# ════════════════════════════════════════════════════════════
# 2. classify_files · 文件名 → category + sku_slug + tags
# ════════════════════════════════════════════════════════════

# 文件名启发规则（rule-only · LLM 兜底待 Block 2.5）
RULE_PATTERNS = {
    "license": re.compile(
        r"(营业执照|资质|证书|license|cert|ISO|FDA|CE|business[\-_\s]?license)",
        re.IGNORECASE,
    ),
    "brand-logo": re.compile(r"(logo|品牌识别|VI|brand[\-_\s]?identity)", re.IGNORECASE),
    "catalog": re.compile(r"(catalog|brochure|画册|产品手册|catalog[ue])", re.IGNORECASE),
    "spec": re.compile(r"(spec|尺寸|规格|dimension|drawing|figure)", re.IGNORECASE),
    "packaging": re.compile(r"(package|包装|礼盒|外箱|box)", re.IGNORECASE),
    "factory": re.compile(r"(工厂|车间|production|factory|生产线|workshop)", re.IGNORECASE),
    "detail": re.compile(r"(detail|细节|特写|closeup|close[\-_\s]?up|material|材质)", re.IGNORECASE),
    "lifestyle": re.compile(r"(lifestyle|场景|scene|model|模特|use[\-_\s]?case)", re.IGNORECASE),
}


def _rule_classify(filename: str, kind: str) -> tuple[str, float, str | None]:
    """规则引擎单文件分类 · 返 (category, confidence, flagged_reason)"""
    # video kind → video category
    if kind == "video":
        return "video", 0.95, None
    if kind in ("audio", "other"):
        return "other", 0.4, "unknown_format"

    for cat, pat in RULE_PATTERNS.items():
        if pat.search(filename):
            return cat, 0.85, None

    # default 兜底
    if kind == "image":
        return "master", 0.5, "low_confidence"
    if kind == "document":
        return "catalog", 0.55, "low_confidence"
    return "other", 0.3, "low_confidence"


def _extract_sku_slug(filename: str) -> str | None:
    """从文件名抽 sku · 用第一段 alpha+digit 组合"""
    base = os.path.splitext(filename)[0]
    # 去掉序号 / 版本号
    base = re.sub(r"[\s_]+(v\d+|final|终稿|修改|修改版|版|copy|副本)\b", "", base, flags=re.IGNORECASE)
    # 取第一段·长度 2-30 字符
    parts = re.split(r"[\s_\-—–·\.]+", base)
    candidates = [p for p in parts if 2 <= len(p) <= 30 and not p.isdigit()]
    if not candidates:
        return None
    return _safe_slug(candidates[0])


@celery_app.task(name="intake.classify_files", bind=True, queue="default")
def classify_files(self, job_id: str) -> dict:
    """跑分类·rule 先跑 · 低置信项（confidence<0.7）走 LLM 兜底"""
    job_uuid = uuid.UUID(job_id)
    with session_scope() as db:
        job = db.get(IntakeJob, job_uuid)
        if not job:
            return {"job_id": job_id, "status": "missing"}

        items = list(
            db.execute(
                select(IntakeItem).where(IntakeItem.job_id == job_uuid)
            ).scalars().all()
        )

        classified = 0
        flagged = 0
        low_conf_items: list[IntakeItem] = []

        for item in items:
            cat, conf, flagged_reason = _rule_classify(item.filename, item.kind or "other")
            sku = _extract_sku_slug(item.filename)
            item.predicted_category = cat
            item.predicted_sku_slug = sku
            item.confidence = conf
            item.flagged_reason = flagged_reason
            tags = [f"factory:{job.factory_slug}"]
            if item.kind:
                tags.append(f"kind:{item.kind}")
            if cat:
                tags.append(f"category:{cat}")
            item.predicted_tags = tags
            classified += 1
            if flagged_reason:
                flagged += 1
            # 收集需要 LLM 复审的（仅 image + document · video/audio 跳）
            if conf < 0.7 and item.kind in ("image", "document"):
                low_conf_items.append(item)
            if classified % 200 == 0:
                db.flush()

        # LLM 兜底分类（仅当 DASHSCOPE_API_KEY 配了且有低置信项）
        llm_upgraded = 0
        llm_cost_total = 0.0
        llm_tokens_in = 0
        llm_tokens_out = 0
        if low_conf_items:
            try:
                llm_upgraded, llm_cost_total, llm_tokens_in, llm_tokens_out = (
                    _llm_classify_batch(job, low_conf_items, db)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "intake.classify.llm_fallback_failed",
                    job_id=job_id, error=str(exc),
                )

        # 累计成本
        if llm_cost_total > 0:
            job.llm_cost_cny = (job.llm_cost_cny or 0) + llm_cost_total
            job.llm_tokens_input = (job.llm_tokens_input or 0) + llm_tokens_in
            job.llm_tokens_output = (job.llm_tokens_output or 0) + llm_tokens_out

        # 重新统计 flagged（LLM 可能修复了部分低置信）
        flagged_after = sum(1 for it in items if it.flagged_reason)
        job.classified_count = classified
        job.flagged_count = flagged_after
        job.status = "clustering"
        db.add(job)

        logger.info(
            "intake.classify.done",
            job_id=job_id, classified=classified,
            flagged_before=flagged, flagged_after=flagged_after,
            llm_upgraded=llm_upgraded, llm_cost_cny=llm_cost_total,
        )
        return {
            "job_id": job_id,
            "classified": classified,
            "flagged": flagged_after,
            "llm_upgraded": llm_upgraded,
            "llm_cost_cny": llm_cost_total,
        }


def _llm_classify_batch(
    job: IntakeJob,
    items: list[IntakeItem],
    db,
    *,
    batch_size: int = 30,
) -> tuple[int, float, int, int]:
    """对低置信项分批送 LLM 复审·返 (upgraded_count, cost_cny, in_tokens, out_tokens)"""
    from app.services import ai_service
    from app.services.intake_prompts import (
        classify_filename_batch_prompt,
    )

    if not ai_service.has_provider():
        return 0, 0.0, 0, 0  # 没 key·静默 skip

    upgraded = 0
    cost_total = 0.0
    in_tok = 0
    out_tok = 0

    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        prompt = classify_filename_batch_prompt(
            job.factory_slug,
            [
                {"id": str(it.id), "name": it.filename, "kind": it.kind or "image"}
                for it in batch
            ],
        )
        # v3 P1.3 phase 5+ (2026-05-14): 改路由版 · intake_extract use_case ·
        # 从 qwen3.6-flash 默认换 qwen-plus first + fallback chain · 提升结构化抽取准确性
        parsed, usage = ai_service.complete_json_for(
            "intake_extract", prompt, max_tokens=2048, temperature=0.1
        )
        cost_total += usage["cost_cny"]
        in_tok += usage["input_tokens"]
        out_tok += usage["output_tokens"]

        if not parsed:
            continue  # LLM 失败·保留 rule 结果

        # LLM 期望返 array 也可能被 wrap 在 {"items": [...]}
        if isinstance(parsed, dict):
            parsed = parsed.get("items") or parsed.get("results") or list(parsed.values())
        if not isinstance(parsed, list):
            continue

        by_id = {str(it.id): it for it in batch}
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            item_id = entry.get("id")
            cat = entry.get("category")
            if cat not in CLASSIFY_CATEGORIES:
                continue
            it = by_id.get(item_id)
            if not it:
                continue
            # 只接受高置信度的改写（不让 LLM 把规则的高置信项给 downgrade）
            new_conf = float(entry.get("confidence", 0))
            if new_conf > (it.confidence or 0):
                it.predicted_category = cat
                if entry.get("sku_slug"):
                    it.predicted_sku_slug = entry["sku_slug"]
                it.confidence = new_conf
                # LLM 提的 tags 合并到现有 tags
                tags_existing = list(it.predicted_tags or [])
                for t in (entry.get("tags") or [])[:5]:
                    if isinstance(t, str) and t not in tags_existing:
                        tags_existing.append(t)
                it.predicted_tags = tags_existing
                # 清 flagged 如果新 confidence 高
                if new_conf >= 0.7:
                    it.flagged_reason = None
                upgraded += 1
        db.flush()

    return upgraded, cost_total, in_tok, out_tok


# ════════════════════════════════════════════════════════════
# 3. cluster_skus · 按 sku_slug 聚类
# ════════════════════════════════════════════════════════════

@celery_app.task(name="intake.cluster_skus", bind=True, queue="default")
def cluster_skus(self, job_id: str) -> dict:
    """按 predicted_sku_slug 聚类 · 同 slug 归一·算 category_breakdown"""
    job_uuid = uuid.UUID(job_id)
    with session_scope() as db:
        job = db.get(IntakeJob, job_uuid)
        if not job:
            return {"job_id": job_id, "status": "missing"}

        items = list(
            db.execute(
                select(IntakeItem).where(
                    IntakeItem.job_id == job_uuid,
                    IntakeItem.predicted_sku_slug.is_not(None),
                )
            ).scalars().all()
        )

        # 按 sku_slug 桶分
        buckets: dict[str, list[IntakeItem]] = {}
        for item in items:
            slug = item.predicted_sku_slug
            buckets.setdefault(slug, []).append(item)

        cluster_count = 0
        for sku_slug, members in buckets.items():
            # category breakdown
            breakdown: dict[str, int] = {}
            for m in members:
                c = m.predicted_category or "uncategorized"
                breakdown[c] = breakdown.get(c, 0) + 1

            # representative: 首选 master · 否则 first
            rep = next(
                (m for m in members if m.predicted_category == "master"),
                members[0],
            )

            cluster = IntakeCluster(
                job_id=job_uuid,
                sku_slug=sku_slug,
                item_count=len(members),
                representative_item_id=rep.id,
                category_breakdown=breakdown,
            )
            db.add(cluster)
            db.flush()  # 拿 cluster.id

            # 回填 cluster_id 到 items
            for m in members:
                m.cluster_id = cluster.id

            cluster_count += 1

        job.clusters_count = cluster_count
        job.status = "parsing_docs"
        db.add(job)

        logger.info("intake.cluster.done", job_id=job_id, clusters=cluster_count)
        return {"job_id": job_id, "clusters": cluster_count}


# ════════════════════════════════════════════════════════════
# 4. parse_docs · 从 catalog / license docx 抽 entity（占位）
# ════════════════════════════════════════════════════════════

@celery_app.task(name="intake.parse_docs", bind=True, queue="default")
def parse_docs(self, job_id: str) -> dict:
    """v4.0 占位：找到第一个 catalog 文档·留待 LLM 解析

    v4.1 接 LLM：
      1. 找 license + catalog items
      2. python-docx / pypdf 抽文本
      3. 调 extract_entity_prompt + qwen-plus
      4. JSON parse 写回 job.entity_yml
    """
    job_uuid = uuid.UUID(job_id)
    with session_scope() as db:
        job = db.get(IntakeJob, job_uuid)
        if not job:
            return {"job_id": job_id, "status": "missing"}

        # 占位 entity_yml：从文件名启发
        license_items = list(
            db.execute(
                select(IntakeItem).where(
                    IntakeItem.job_id == job_uuid,
                    IntakeItem.predicted_category == "license",
                )
            ).scalars().all()
        )
        catalog_items = list(
            db.execute(
                select(IntakeItem).where(
                    IntakeItem.job_id == job_uuid,
                    IntakeItem.predicted_category == "catalog",
                )
            ).scalars().all()
        )

        job.entity_yml = {
            "factory_slug": job.factory_slug,
            "license_files": [it.filename for it in license_items[:5]],
            "catalog_files": [it.filename for it in catalog_items[:5]],
            "extraction_notes": "v4.0 rule-only · 待 LLM 抽 entity（Block 2.5）",
        }
        job.status = "finalizing"
        db.add(job)

        logger.info(
            "intake.parse_docs.done",
            job_id=job_id, license_count=len(license_items), catalog_count=len(catalog_items),
        )
        return {
            "job_id": job_id,
            "license_count": len(license_items),
            "catalog_count": len(catalog_items),
        }


# ════════════════════════════════════════════════════════════
# 5. finalize · 算 predicted_subdir + predicted_target_filename · 翻 reviewing
# ════════════════════════════════════════════════════════════

@celery_app.task(name="intake.finalize", bind=True, queue="default")
def finalize_intake(self, job_id: str) -> dict:
    """给每个 item 算最终 DAM 路径 · 标 reviewing 等用户 review"""
    job_uuid = uuid.UUID(job_id)
    with session_scope() as db:
        job = db.get(IntakeJob, job_uuid)
        if not job:
            return {"job_id": job_id, "status": "missing"}

        items = list(
            db.execute(
                select(IntakeItem).where(IntakeItem.job_id == job_uuid)
            ).scalars().all()
        )

        # 按 sku 累计序号
        counter: dict[tuple[str, str], int] = {}

        for item in items:
            cat = item.predicted_category or "other"
            sku = item.predicted_sku_slug or "uncategorized"
            ext = os.path.splitext(item.filename)[1].lower() or ".bin"

            # subdir：/factories/{factory}/sku/{sku}/{category}/
            if cat in ("license", "brand-logo", "factory", "catalog"):
                # 工厂级别·不挂 sku
                subdir = f"/factories/{job.factory_slug}/{cat}/"
            else:
                subdir = f"/factories/{job.factory_slug}/sku/{sku}/{cat}/"

            key = (sku, cat)
            counter[key] = counter.get(key, 0) + 1
            seq = counter[key]
            # 命名：{sku}--{category}--{NN}.{ext}
            target = f"{sku}--{cat}--{seq:02d}{ext}"

            item.predicted_subdir = subdir
            item.predicted_target_filename = target

        job.status = "reviewing"
        job.review_at = datetime.now(timezone.utc)
        db.add(job)

        logger.info(
            "intake.finalize.done",
            job_id=job_id, items=len(items),
        )
        return {"job_id": job_id, "items": len(items), "status": "reviewing"}


# ════════════════════════════════════════════════════════════
# 6. run_intake_pipeline · chain orchestrator
# ════════════════════════════════════════════════════════════

@celery_app.task(name="intake.run_pipeline", bind=True, queue="default")
def run_intake_pipeline(self, job_id: str) -> dict:
    """Pipeline 入口·全部用 immutable signatures 防中间失败阻塞 finalize"""
    sig_chain = chain(
        celery_app.signature("intake.scan_files", args=[job_id], immutable=True),
        celery_app.signature("intake.classify_files", args=[job_id], immutable=True),
        celery_app.signature("intake.cluster_skus", args=[job_id], immutable=True),
        celery_app.signature("intake.parse_docs", args=[job_id], immutable=True),
        celery_app.signature("intake.finalize", args=[job_id], immutable=True),
    )
    sig_chain.apply_async()
    return {"job_id": job_id, "status": "pipeline_enqueued"}


# ════════════════════════════════════════════════════════════
# 7. push_to_dam · approve 后真上线·占位（v4.1 实装）
# ════════════════════════════════════════════════════════════

@celery_app.task(name="intake.push_to_dam", bind=True, queue="default")
def push_to_dam(self, job_id: str) -> dict:
    """approve 后把 items 真传到 DAM·v4.1 实装

    流程·每个 item：
      1. 计算 storage_key（用现有 storage.build_storage_key）
      2. boto3 put_object 把原文件传 R2
      3. 写 Asset 行（status=processing·让 pipeline 接管缩略图）
      4. 触发 process_pipeline.delay(asset_id) · 缩略图 + ai 自动跑
      5. 写 audit + bump usage + intake_items.pushed_asset_id 关联

    防呆：
      - 同 sha256 已存在 asset（跨 project / job 跑过）→ 跳过新建·直接关联老 asset_id
      - put_object 失败 → push_error 记录·继续下一个（不让 1 文件挂全 job）
      - 任意失败 → job.push_error_count++
    """

    from app.core.config import settings
    from app.models.asset import Asset
    from app.models.project import Project
    from app.models.tenant import Tenant
    from app.services import storage
    from app.services.asset_service import classify_kind, safe_extension

    job_uuid = uuid.UUID(job_id)
    with session_scope() as db:
        job = db.get(IntakeJob, job_uuid)
        if not job:
            return {"job_id": job_id, "status": "missing"}

        if job.status != "approved":
            logger.warning(
                "intake.push.skip_not_approved",
                job_id=job_id, current_status=job.status,
            )
            return {"job_id": job_id, "status": "skipped", "reason": job.status}

        # 翻 pushing
        job.status = "pushing"
        db.add(job)
        db.flush()

        tenant = db.get(Tenant, job.tenant_id)
        project = db.get(Project, job.project_id)
        if not tenant or not project:
            job.status = "failed"
            job.failed_reason = "tenant or project not found"
            job.completed_at = datetime.now(timezone.utc)
            db.add(job)
            return {"job_id": job_id, "status": "failed", "reason": "missing_tenant_or_project"}

        # 待推 items：user_decision='approve' OR 'edit' OR null（默认全 approve）
        items: list[IntakeItem] = list(
            db.execute(
                select(IntakeItem).where(
                    IntakeItem.job_id == job_uuid,
                    IntakeItem.user_decision.in_(("approve", "edit", None)),
                    IntakeItem.pushed_asset_id.is_(None),
                    IntakeItem.user_decision != "reject",
                )
            ).scalars().all()
        )

        pushed = 0
        errors = 0
        for it in items:
            try:
                # 1) dedup check by sha256 within project
                existing = db.execute(
                    select(Asset).where(
                        Asset.project_id == project.id,
                        Asset.sha256 == it.sha256,
                        Asset.deleted_at.is_(None),
                    ).limit(1)
                ).scalar_one_or_none()

                if existing:
                    # 已有同 sha256 asset · 关联不再上传
                    it.pushed_asset_id = existing.id
                    it.pushed_at = datetime.now(timezone.utc)
                    it.push_error = None
                    pushed += 1
                    logger.info(
                        "intake.push.dedup_hit",
                        item_id=str(it.id), existing_asset=str(existing.id),
                    )
                    continue

                # 2) 计算 storage_key + 抽 extension/kind/mime
                asset_id = uuid.uuid4()
                target_name = it.predicted_target_filename or it.filename
                ext = safe_extension(target_name, it.mime_type or "")
                kind = classify_kind(it.mime_type or "", ext)
                storage_key = storage.build_storage_key(
                    tenant_storage_prefix=tenant.storage_prefix,
                    project_storage_prefix=project.storage_prefix,
                    asset_id=asset_id,
                    extension=ext,
                )

                # 3) 真上传 · 流式读·防 OOM
                try:
                    with open(it.source_path, "rb") as fp:
                        body = fp.read()  # v4.1 用 stream multipart for >100MB
                except OSError as exc:
                    it.push_error = f"read_source_failed: {exc}"
                    errors += 1
                    continue

                try:
                    storage.put_object(
                        storage_key=storage_key,
                        body=body,
                        content_type=it.mime_type or "application/octet-stream",
                    )
                except Exception as exc:  # noqa: BLE001
                    it.push_error = f"r2_put_failed: {str(exc)[:200]}"
                    errors += 1
                    continue

                # 4) 写 Asset 行 · status=processing 让 pipeline 接管缩略图/AI
                tags = list(it.predicted_tags or [])
                if it.predicted_category and f"category:{it.predicted_category}" not in tags:
                    tags.append(f"category:{it.predicted_category}")
                if it.predicted_sku_slug and f"sku:{it.predicted_sku_slug}" not in tags:
                    tags.append(f"sku:{it.predicted_sku_slug}")
                tags.append(f"intake-job:{job.id}")
                tags.append(f"factory:{job.factory_slug}")

                asset = Asset(
                    id=asset_id,
                    tenant_id=job.tenant_id,
                    project_id=job.project_id,
                    name=target_name,
                    sha256=it.sha256,
                    kind=kind,
                    mime_type=it.mime_type,
                    extension=ext,
                    size_bytes=it.size_bytes,
                    storage_key=storage_key,
                    storage_bucket=settings.S3_BUCKET,
                    public_url=None,
                    status="processing",
                    source="intake",
                    acl=project.default_acl,
                    manual_tags=tags,
                )
                db.add(asset)
                db.flush()

                # 5) 关联回 intake_item
                it.pushed_asset_id = asset.id
                it.pushed_at = datetime.now(timezone.utc)
                it.push_error = None
                pushed += 1

                # 6) bump usage 计费 · 直接 sync UPSERT（不走 async usage_service.bump）
                try:
                    from sqlalchemy import text as _sql_text
                    db.execute(
                        _sql_text(
                            """
                            INSERT INTO usage_meters (
                                tenant_id, day,
                                storage_bytes_total, upload_bytes, download_bytes,
                                new_asset_count, ai_calls,
                                ai_input_tokens, ai_output_tokens, webhook_deliveries
                            )
                            VALUES (
                                :tenant_id, CURRENT_DATE,
                                :bytes, :bytes, 0,
                                1, 0, 0, 0, 0
                            )
                            ON CONFLICT (tenant_id, day) DO UPDATE SET
                                storage_bytes_total = usage_meters.storage_bytes_total + EXCLUDED.storage_bytes_total,
                                upload_bytes = usage_meters.upload_bytes + EXCLUDED.upload_bytes,
                                new_asset_count = usage_meters.new_asset_count + EXCLUDED.new_asset_count,
                                updated_at = NOW()
                            """
                        ),
                        {"tenant_id": str(job.tenant_id), "bytes": it.size_bytes},
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "intake.push.usage_bump_failed",
                        item_id=str(it.id), error=str(exc),
                    )

                # 7) 入 pipeline 缩略图 + AI
                try:
                    from app.workers.tasks_pipeline import process_pipeline
                    process_pipeline.delay(str(asset.id))
                except Exception as exc:  # noqa: BLE001
                    # pipeline 调度失败 · asset 留 processing · 不影响 push 成功
                    logger.warning(
                        "intake.push.pipeline_enqueue_failed",
                        asset_id=str(asset.id), error=str(exc),
                    )

            except Exception as exc:  # noqa: BLE001
                it.push_error = f"unexpected: {str(exc)[:200]}"
                errors += 1
                logger.error(
                    "intake.push.item_failed",
                    item_id=str(it.id), error=str(exc),
                )

        job.pushed_count = pushed
        job.push_error_count = errors
        job.status = "pushed" if errors == 0 else (
            "pushed" if pushed > 0 else "failed"
        )
        if errors > 0 and pushed == 0:
            job.failed_reason = f"all {errors} items failed to push"
        elif errors > 0:
            job.failed_reason = f"{errors}/{pushed + errors} items had errors (partial success)"
        job.completed_at = datetime.now(timezone.utc)
        db.add(job)

        logger.info(
            "intake.push.done",
            job_id=job_id, pushed=pushed, errors=errors,
        )
        return {
            "job_id": job_id,
            "pushed": pushed,
            "errors": errors,
            "status": job.status,
        }
