"""/v1/crm/dashboard · 销售仪表盘 endpoint

返回完整 360 度 view：
  - 本月新询盘 + 同比涨跌
  - A 类待跟进
  - 活跃 deals + forecast
  - 本月成交
  - 漏斗（leads → qualified → deals → won）
  - 渠道分布
  - Top 工厂
  - 分类分布
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, require_authenticated
from app.db.session import get_db
from app.services.crm import dashboard_service

router = APIRouter()


@router.get("/")
async def get_dashboard(
    factory_slug: Optional[str] = Query(None, description="筛选单工厂"),
    principal: Principal = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """CRM 销售仪表盘聚合 API"""
    return await dashboard_service.get_dashboard_summary(
        db,
        tenant_id=principal.tenant_id,
        factory_slug=factory_slug,
    )
