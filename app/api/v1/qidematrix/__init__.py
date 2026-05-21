"""QideMatrix REST routers

v0：workspaces / subscriptions / sso / social / topics
v1（2026-05-21）· 8 阶段业务流：
  onboardings (S1)
  diagnostics (S2)
  orders + quotes (S6 + S7)
  health (S8)
  pipeline + emails (事件总线 + 邮件 outbox)
"""
from app.api.v1.qidematrix import (
    diagnostics,
    health,
    onboardings,
    orders,
    pipeline,
    social,
    sso,
    subscriptions,
    topics,
    workspaces,
)

__all__ = [
    # v0
    "workspaces", "subscriptions", "sso", "social", "topics",
    # v1
    "onboardings", "diagnostics", "orders", "health", "pipeline",
]
