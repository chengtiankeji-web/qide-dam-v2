"""CRM 模块·API 路由聚合

挂载位置（在 app/api/v1/__init__.py 加）：
  from app.api.v1.crm import crm_router
  api_router.include_router(crm_router, prefix="/crm", tags=["crm"])

子路由：
  /v1/crm/leads/*           询盘（含 6 要素分级 + 状态机 + lead→deal 转换）
  /v1/crm/contacts/*        联系人（含 dedup + opt-in 管理）
  /v1/crm/accounts/*        公司（含 dedup + merge）
  /v1/crm/deals/*           商机（含 pipeline 状态机 + forecast）
  /v1/crm/dashboard/*       销售仪表盘聚合
  /v1/crm/quotes/*          报价单（v7.1 待写·含 PDF 生成）
  /v1/crm/activities/*      活动 timeline（v7.1 待写）
  /v1/crm/emails/*          邮件营销（v7.1 待写·Resend 集成）
"""
from fastapi import APIRouter

from app.api.v1.crm import (
    accounts,
    activities,
    contacts,
    dashboard,
    deals,
    emails,
    leads,
    quotes,
)

crm_router = APIRouter()
crm_router.include_router(leads.router, prefix="/leads", tags=["crm-leads"])
crm_router.include_router(contacts.router, prefix="/contacts", tags=["crm-contacts"])
crm_router.include_router(accounts.router, prefix="/accounts", tags=["crm-accounts"])
crm_router.include_router(deals.router, prefix="/deals", tags=["crm-deals"])
crm_router.include_router(quotes.router, prefix="/quotes", tags=["crm-quotes"])
crm_router.include_router(emails.router, prefix="/emails", tags=["crm-emails"])
crm_router.include_router(activities.router, prefix="/activities", tags=["crm-activities"])
crm_router.include_router(dashboard.router, prefix="/dashboard", tags=["crm-dashboard"])
