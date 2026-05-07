"""v1 router aggregator."""
from fastapi import APIRouter

from app.api.v1 import (
    assets,
    audit,
    auth,
    collections,
    folders,
    health,
    projects,
    search,
    share_links,
    tenants,
    uploads,
    usage,
    users,
    vault,
    webhooks,
    wecom,
    workflows,
)

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
