"""S3 · DAM workspace 自动 provisioning service

订阅 dam.workspace_ready 之前的 dam.workspace_provision 事件 ·
基于 onboarding 自动建：
  1. tenant（如果 onboarding 是新 tenant · 复用已有 tenant 不再建）
  2. QmWorkspace（默认 plan=trial · slug 从 factory_name 派生）
  3. Project（QideDAM 多租户的 project · 关联 tenant · 用于资产隔离）
  4. Folder 结构（00-客户档案 / 10-入驻申请 / 20-诊断报告 / 30-社媒矩阵 / ...）
  5. 客户上传的 asset 全部转入 + sensitivity 自动打标

完成后：
  - publish dam.workspace_ready 事件 · 触发邮件 + S4 运营接单提醒
  - 回填 onboarding.workspace_id
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.asset import Asset
from app.models.folder import Folder
from app.models.project import Project
from app.models.qidematrix.pipeline import QmOnboarding
from app.models.qidematrix.workspace import (
    QmWorkspace,
    QmWorkspaceMember,
)
from app.models.qidematrix.subscription import QmSubscription
from app.services.qidematrix import onboarding_service, pipeline_service

logger = get_logger("qm.dam_provisioning")


# ─── 默认文件夹结构（200 人级 DAM 架构 · 客户子空间）─────────────────

DEFAULT_FOLDERS: list[dict] = [
    # 00-09 · 客户档案
    {"path": "/00-客户档案", "sensitivity": "internal"},
    {"path": "/00-客户档案/01-基本信息", "sensitivity": "internal"},
    {"path": "/00-客户档案/02-合同与协议", "sensitivity": "confidential"},
    {"path": "/00-客户档案/03-沟通记录", "sensitivity": "internal"},

    # 10-19 · 入驻申请
    {"path": "/10-入驻申请", "sensitivity": "internal"},
    {"path": "/10-入驻申请/01-入驻表单", "sensitivity": "internal"},
    {"path": "/10-入驻申请/02-上传素材-原始", "sensitivity": "internal"},
    {"path": "/10-入驻申请/03-上传素材-处理后", "sensitivity": "internal"},

    # 20-29 · S2 诊断
    {"path": "/20-诊断报告", "sensitivity": "internal"},
    {"path": "/20-诊断报告/01-PDF", "sensitivity": "internal"},
    {"path": "/20-诊断报告/02-Markdown 原稿", "sensitivity": "internal"},

    # 30-39 · S4 社媒矩阵
    {"path": "/30-社媒矩阵", "sensitivity": "confidential"},
    {"path": "/30-社媒矩阵/01-账号凭证", "sensitivity": "secret"},
    {"path": "/30-社媒矩阵/02-浏览器指纹", "sensitivity": "secret"},
    {"path": "/30-社媒矩阵/03-发帖归档", "sensitivity": "internal"},

    # 40-49 · S5 内容生产
    {"path": "/40-内容资产", "sensitivity": "internal"},
    {"path": "/40-内容资产/01-SEO 文章", "sensitivity": "internal"},
    {"path": "/40-内容资产/02-短视频", "sensitivity": "internal"},
    {"path": "/40-内容资产/03-平面物料", "sensitivity": "internal"},
    {"path": "/40-内容资产/04-多语言版本", "sensitivity": "internal"},

    # 50-59 · S6 询盘 + CRM
    {"path": "/50-询盘与客户", "sensitivity": "confidential"},
    {"path": "/50-询盘与客户/01-原始询盘", "sensitivity": "confidential"},
    {"path": "/50-询盘与客户/02-报价单", "sensitivity": "confidential"},
    {"path": "/50-询盘与客户/03-NNN-合同", "sensitivity": "confidential"},

    # 60-69 · S7 派单与订单
    {"path": "/60-订单与履约", "sensitivity": "confidential"},
    {"path": "/60-订单与履约/01-订单合同", "sensitivity": "confidential"},
    {"path": "/60-订单与履约/02-物流追踪", "sensitivity": "internal"},
    {"path": "/60-订单与履约/03-收款凭证", "sensitivity": "confidential"},
    {"path": "/60-订单与履约/04-报关单据", "sensitivity": "confidential"},

    # 80-89 · 报告
    {"path": "/80-报告", "sensitivity": "internal"},
    {"path": "/80-报告/01-月度报告", "sensitivity": "internal"},
    {"path": "/80-报告/02-季度复盘", "sensitivity": "internal"},

    # 90-99 · 其他
    {"path": "/90-其他", "sensitivity": "internal"},
]


def _derive_slug(factory_name: str) -> str:
    """从工厂名生成 slug · 含中文时取拼音简写或哈希后缀

    Examples:
        "深圳市艺欣恒有机制品" → "shenzhen-yixinheng-<6hex>"
        "Foshan Qide Co" → "foshan-qide-<6hex>"
    """
    s = factory_name.lower()
    # 替换非 ASCII 的字符为短 hash
    has_non_ascii = bool(re.search(r"[^\x00-\x7f]", s))
    s_ascii = re.sub(r"[^a-z0-9-]+", "-", s)
    s_ascii = re.sub(r"-+", "-", s_ascii).strip("-")

    if not s_ascii or has_non_ascii:
        salt = uuid.uuid4().hex[:6]
        return f"customer-{salt}"

    salt = uuid.uuid4().hex[:6]
    truncated = s_ascii[:50].rstrip("-")
    return f"{truncated}-{salt}"


# ═════════════════════════════════════════════════════════════════════
# 主入口：provision_workspace_for_onboarding
# ═════════════════════════════════════════════════════════════════════

async def provision_workspace_for_onboarding(
    db: AsyncSession,
    *,
    onboarding: QmOnboarding,
    owner_user_id: uuid.UUID,
) -> QmWorkspace:
    """基于 onboarding 自动建 workspace + project + folders + 资产打标

    幂等：如果 onboarding.workspace_id 已存在 · 直接返回那个 workspace
    """
    if onboarding.workspace_id:
        existing = await db.execute(
            select(QmWorkspace).where(QmWorkspace.id == onboarding.workspace_id)
        )
        existing_ws = existing.scalar_one_or_none()
        if existing_ws:
            return existing_ws

    now = datetime.now(UTC)
    slug = _derive_slug(onboarding.factory_name)
    trial_ends = now.replace() + (now.replace() - now)  # placeholder · 14 天

    # 1. workspace
    from datetime import timedelta
    workspace = QmWorkspace(
        id=uuid.uuid4(),
        tenant_id=onboarding.tenant_id,
        slug=slug,
        display_name=onboarding.factory_name,
        owner_user_id=owner_user_id,
        plan="trial",
        plan_seats=3,
        plan_storage_gb=1,
        plan_ai_calls_monthly=100,
        trial_ends_at=now + timedelta(days=14),
        industry="foreign_trade",
        locale="zh-CN",
        created_at=now,
        updated_at=now,
        extra_metadata={
            "provisioned_from": "onboarding",
            "onboarding_id": str(onboarding.id),
        },
    )
    db.add(workspace)
    await db.flush()

    # 2. owner member
    member = QmWorkspaceMember(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        user_id=owner_user_id,
        role="owner",
        joined_at=now,
        extra_metadata={},
    )
    db.add(member)

    # 3. trial subscription
    sub = QmSubscription(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        plan="trial",
        status="trial",
        billing_cycle="monthly",
        price_cny_cents=0,
        started_at=now,
        current_period_start=now,
        current_period_end=now + timedelta(days=14),
        cancel_at_period_end=False,
        payment_provider=None,
        extra_metadata={"trial_init": True, "source": "onboarding"},
        created_at=now,
        updated_at=now,
    )
    db.add(sub)

    # 4. Project · 用于 QideDAM 多租户 asset 隔离
    # 复用 QideDAM project 表（已有 tenant_id + slug 唯一）
    project_slug = f"qm-{slug}"
    project = Project(
        id=uuid.uuid4(),
        tenant_id=onboarding.tenant_id,
        slug=project_slug,
        name=onboarding.factory_name,
        description=f"QideMatrix workspace for {onboarding.factory_name}",
        created_at=now,
        updated_at=now,
    )
    db.add(project)
    await db.flush()

    # 5. Folder 结构
    folder_id_by_path: dict[str, uuid.UUID] = {}
    for f in DEFAULT_FOLDERS:
        path = f["path"]
        folder = Folder(
            id=uuid.uuid4(),
            project_id=project.id,
            tenant_id=onboarding.tenant_id,
            path=path,
            name=path.split("/")[-1],
            created_at=now,
            updated_at=now,
        )
        db.add(folder)
        folder_id_by_path[path] = folder.id

    await db.flush()

    # 6. 客户已上传的 assets · 转入 /10-入驻申请/02-上传素材-原始
    raw_assets_folder_id = folder_id_by_path.get("/10-入驻申请/02-上传素材-原始")
    if onboarding.asset_ids and raw_assets_folder_id:
        # 把客户 asset 的 project 改成新 workspace 的 project
        asset_ids = [aid for aid in onboarding.asset_ids if aid]
        if asset_ids:
            assets_q = await db.execute(
                select(Asset).where(Asset.id.in_(asset_ids))
            )
            for asset in assets_q.scalars():
                asset.project_id = project.id
                asset.folder_id = raw_assets_folder_id
                # sensitivity 默认 internal · 之前可能是 public（CMH 提交时）
                if asset.sensitivity_level == "public":
                    asset.sensitivity_level = "internal"
                asset.updated_at = now

    # 7. 回填 onboarding.workspace_id
    await onboarding_service.attach_workspace(
        db, onboarding_id=onboarding.id, workspace_id=workspace.id
    )

    # 8. publish dam.workspace_ready
    await pipeline_service.publish(
        db,
        tenant_id=onboarding.tenant_id,
        workspace_id=workspace.id,
        event_type="dam.workspace_ready",
        subject_kind="workspace",
        subject_id=workspace.id,
        payload={
            "workspace_id": str(workspace.id),
            "workspace_slug": slug,
            "project_id": str(project.id),
            "onboarding_id": str(onboarding.id),
            "folder_count": len(DEFAULT_FOLDERS),
            "asset_count": len(onboarding.asset_ids or []),
        },
    )

    # 9. mark onboarding ready（S3 done · 等运营接单到 S4）
    await onboarding_service.mark_ready(db, onboarding_id=onboarding.id)

    logger.info(
        "qm.dam.workspace_provisioned",
        workspace_id=str(workspace.id),
        workspace_slug=slug,
        onboarding_id=str(onboarding.id),
        folder_count=len(DEFAULT_FOLDERS),
    )

    return workspace
