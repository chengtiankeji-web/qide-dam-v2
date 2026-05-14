"""CRM ↔ DAM 整合服务

让 CRM 客户工作流深度复用 QideDAM 资产：
1. quote line_items → 关联 DAM asset_id（产品 master 图）· PDF 自动嵌图
2. quote PDF 生成时 · 引用 DAM URL 变换 API（v4.5 后）
3. lead 来源 share_link 自动反查·写到 lead.source_share_link_id
4. activity attachments → DAM asset_id 引用
5. Brand Portal 显示客户工厂的资产（v4.5 后）

v7 MVP 实装：1 + 5 部分
v7.1 起：2 / 3 / 4
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

logger = get_logger(__name__)


async def link_quote_items_to_dam_assets(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    line_items: list[dict],
    factory_slug: str | None = None,
) -> list[dict]:
    """quote line_items · 自动查 DAM 里同 sku-slug 的 master 图 · 写到 line_item

    入参: line_items 列表 · 每条可能有 sku_slug
    出参: 同列表 · 每条加 `master_asset_id` + `master_thumb_url` 字段（如能匹配到）

    用法（quotes_service.create_quote 末尾调）：
        line_items = await link_quote_items_to_dam_assets(
            db, tenant_id=tenant_id, line_items=line_items, factory_slug=factory_slug,
        )
    """
    if not line_items:
        return line_items

    # 收集要查的 sku_slugs
    sku_slugs = [
        it.get("sku_slug")
        for it in line_items
        if it.get("sku_slug")
    ]
    if not sku_slugs:
        return line_items

    # 查 DAM assets · 找 tag matches "sku:<slug>" + category:master + status=ready
    from app.models.asset import Asset

    q = (
        select(Asset)
        .where(
            Asset.tenant_id == tenant_id,
            Asset.status == "ready",
            Asset.kind == "image",
        )
    )
    result = await db.execute(q)
    all_assets = list(result.scalars().all())

    # 按 sku_slug 索引
    sku_to_master: dict[str, Asset] = {}
    for asset in all_assets:
        tags = asset.manual_tags or []
        if "category:master" not in tags:
            continue
        for tag in tags:
            if tag.startswith("sku:"):
                slug = tag[4:]
                if slug in sku_slugs and slug not in sku_to_master:
                    sku_to_master[slug] = asset

    # 回写 line_items
    for it in line_items:
        slug = it.get("sku_slug")
        if slug and slug in sku_to_master:
            asset = sku_to_master[slug]
            it["master_asset_id"] = str(asset.id)
            # 缩略图 URL（DAM 已生 sm/md/lg 缩略图）
            thumbs = asset.thumbnails or {}
            it["master_thumb_url"] = thumbs.get("md") or thumbs.get("sm")
            it["master_filename"] = asset.name

    return line_items


async def get_factory_brand_assets(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    factory_slug: str,
) -> dict:
    """读工厂的品牌资产·给 Brand Portal / quote letterhead / email signature 用

    返回（按 category 聚合）：
      {
        "logo": [Asset],           # category:brand-logo
        "licenses": [Asset],       # category:license · sensitivity ≤ internal
        "factory_photos": [Asset], # category:factory
        "catalog": [Asset],        # category:catalog (PDF)
      }
    """
    from app.models.asset import Asset

    factory_tag = f"entity:{factory_slug}"

    q = (
        select(Asset)
        .where(
            Asset.tenant_id == tenant_id,
            Asset.status == "ready",
            Asset.manual_tags.contains([factory_tag]),
        )
        .limit(500)
    )
    result = await db.execute(q)
    all_assets = list(result.scalars().all())

    bucket = {
        "logo": [],
        "licenses": [],
        "factory_photos": [],
        "catalog": [],
        "master_thumbs": [],
    }
    for asset in all_assets:
        tags = asset.manual_tags or []
        sensitivity = next(
            (t[12:] for t in tags if t.startswith("sensitivity:")),
            "internal",
        )
        # 公开 brand portal 不返 confidential / secret
        if sensitivity in ("confidential", "secret"):
            continue

        if "category:brand-logo" in tags:
            bucket["logo"].append(asset)
        elif "category:license" in tags:
            bucket["licenses"].append(asset)
        elif "category:factory" in tags:
            bucket["factory_photos"].append(asset)
        elif "category:catalog" in tags:
            bucket["catalog"].append(asset)
        elif "category:master" in tags:
            bucket["master_thumbs"].append(asset)

    return bucket


async def attach_dam_assets_to_activity(
    db: AsyncSession,
    *,
    activity_id: uuid.UUID,
    asset_ids: list[uuid.UUID],
) -> None:
    """把 DAM assets 关联到 crm_activity.attachments 字段

    场景：BD 在 lead 详情页传文件 → 上 DAM → 关联到 activity
    """
    from app.models.asset import Asset
    from app.models.crm.activity import CRMActivity

    activity = await db.get(CRMActivity, activity_id)
    if not activity:
        raise ValueError(f"Activity {activity_id} not found")

    # 拉 assets metadata
    q = select(Asset).where(Asset.id.in_(asset_ids))
    result = await db.execute(q)
    assets = list(result.scalars().all())

    attachments_data = [
        {
            "asset_id": str(a.id),
            "filename": a.name,
            "mime": a.mime_type,
            "size_bytes": a.size_bytes,
            "thumb_url": (a.thumbnails or {}).get("sm"),
        }
        for a in assets
    ]
    activity.attachments = attachments_data
    await db.flush()


async def find_share_link_for_lead(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    share_link_token: str,
) -> uuid.UUID | None:
    """share_link 公开 resolve · 反查 share_link.id

    用于：客户点了 brand portal 的"询盘"按钮（带 ?share_link=<token>）·
    系统自动写 lead.source_share_link_id 关联回 DAM 资产
    """
    from app.models.share_link import ShareLink

    q = select(ShareLink).where(
        ShareLink.tenant_id == tenant_id,
        ShareLink.token == share_link_token,
    )
    result = await db.execute(q)
    link = result.scalar_one_or_none()
    return link.id if link else None
