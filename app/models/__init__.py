"""Importing all models in one place so Alembic autogenerate sees them."""
from app.models.api_key import ApiKey
from app.models.asset import Asset, AssetVersion
from app.models.audit import AuditEvent
from app.models.collection import Collection, CollectionAsset
from app.models.folder import Folder
from app.models.intake import IntakeCluster, IntakeItem, IntakeJob
from app.models.project import Project
from app.models.r2_orphan import R2Orphan  # v3 P1.3 (2026-05-13)
from app.models.share_link import ShareLink
from app.models.social import SocialAccount, SocialCredential, SocialPost
from app.models.tenant import Tenant
from app.models.usage_meter import UsageMeter
from app.models.user import User
from app.models.vault import VaultItem, VaultKeyMaterial
from app.models.webhook import MultipartUpload, WebhookDelivery, WebhookSubscription
from app.models.workflow import Workflow, WorkflowStep

__all__ = [
    "ApiKey",
    "Asset",
    "AssetVersion",
    "AuditEvent",
    "Collection",
    "CollectionAsset",
    "Folder",
    "IntakeCluster",
    "IntakeItem",
    "IntakeJob",
    "MultipartUpload",
    "Project",
    "R2Orphan",
    "ShareLink",
    "SocialAccount",
    "SocialCredential",
    "SocialPost",
    "Tenant",
    "UsageMeter",
    "User",
    "VaultItem",
    "VaultKeyMaterial",
    "WebhookDelivery",
    "WebhookSubscription",
    "Workflow",
    "WorkflowStep",
]
