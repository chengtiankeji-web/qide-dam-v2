"""v1 router aggregator."""
from fastapi import APIRouter

from app.api.v1 import (
    assets,
    audit,
    auth,
    collections,
    consolidate,  # v3 P1.3 #5 (2026-05-13 晚)
    folders,
    health,
    intake,
    projects,
    search,
    share_links,
    social,
    tenants,
    uploads,
    usage,
    users,
    vault,
    webhooks,
    wecom,
    workflows,
)
from app.api.v1.crm import crm_router

api_router = APIRouter(prefix="/v1")
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(tenants.router, prefix="/tenants", tags=["tenants"])
api_router.include_router(projects.router, prefix="/projects", tags=["projects"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(assets.router, prefix="/assets", tags=["assets"])
api_router.include_router(uploads.router, prefix="/uploads", tags=["uploads"])
api_router.include_router(search.router, prefix="/search", tags=["search"])
api_router.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
api_router.include_router(collections.router, prefix="/collections", tags=["collections"])
api_router.include_router(folders.router, prefix="/folders", tags=["folders"])
api_router.include_router(workflows.router, prefix="/workflows", tags=["workflows"])
api_router.include_router(share_links.router, prefix="/share-links", tags=["share-links"])
api_router.include_router(usage.router, prefix="/usage", tags=["usage"])
api_router.include_router(wecom.router, prefix="/wecom", tags=["wecom"])

# v3 P0-1 + P0-2
api_router.include_router(vault.router, tags=["vault"])
api_router.include_router(audit.router, tags=["audit"])

# v4 Smart Intake
api_router.include_router(intake.router, prefix="/intake", tags=["intake"])

# v4 Social Matrix
api_router.include_router(social.router, prefix="/social", tags=["social"])

# v7 CRM (leads + contacts + accounts + deals + quotes + emails + activities + dashboard)
api_router.include_router(crm_router, prefix="/crm")

# v3 P1.3 #5 (2026-05-13 晚): handover/plans/sources 消化到 memory
api_router.include_router(consolidate.router, prefix="/consolidate", tags=["consolidate"])
