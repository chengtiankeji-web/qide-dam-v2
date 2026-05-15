"""QideMatrix v0 REST routers · workspaces / subscriptions / sso / social

注册到 app.api.v1.__init__：
    from app.api.v1.qidematrix import sso, subscriptions, workspaces, social
    api_router.include_router(workspaces.router)
    api_router.include_router(subscriptions.router)
    api_router.include_router(sso.router)
    api_router.include_router(social.router)
"""
from app.api.v1.qidematrix import social, sso, subscriptions, workspaces

__all__ = ["workspaces", "subscriptions", "sso", "social"]
