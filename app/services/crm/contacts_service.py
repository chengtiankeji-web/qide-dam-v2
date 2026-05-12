"""contacts_service · 联系人 CRUD + 智能 dedup + opt-in 管理"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal
from app.core.logging import get_logger
from app.models.crm.account import Account
from app.models.crm.contact import Contact
from app.services import audit_service
from app.services.audit_service import AuditAction

logger = get_logger(__name__)


async def create_contact(
    db: AsyncSession,
    *,
    principal: Principal,
    tenant_id: uuid.UUID,
    full_name: str,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    title: Optional[str] = None,
    role_category: Optional[str] = None,
    account_id: Optional[uuid.UUID] = None,
    linkedin_url: Optional[str] = None,
    source: Optional[str] = None,
    dedup_check: bool = True,
) -> Contact:
    """创建联系人·默认按 (tenant_id, email) 去重·已存在则返回旧 row + 更新缺失字段"""
    if dedup_check and email:
        existing = await find_by_email(db, tenant_id=tenant_id, email=email)
        if existing:
            # 智能合并：填缺失字段·不覆盖已有值
            changed = False
            if not existing.full_name and full_name:
                existing.full_name = full_name; changed = True
            if not existing.title and title:
                existing.title = title; changed = True
            if not existing.phone and phone:
                existing.phone = phone; changed = True
            if not existing.account_id and account_id:
                existing.account_id = account_id; changed = True
            if not existing.linkedin_url and linkedin_url:
                existing.linkedin_url = linkedin_url; changed = True
            if not existing.role_category and role_category:
                existing.role_category = role_category; changed = True
            if changed:
                await db.flush()
            return existing

    # 新建
    parts = full_name.strip().split(maxsplit=1)
    first_name = parts[0] if parts else None
    last_name = parts[1] if len(parts) > 1 else None

    contact = Contact(
        tenant_id=tenant_id,
        account_id=account_id,
        first_name=first_name,
        last_name=last_name,
        full_name=full_name,
        title=title,
        role_category=role_category or _infer_role_category(title),
        email=email,
        phone=phone,
        linkedin_url=linkedin_url,
        source=source,
        created_by_user_id=principal.user_id,
    )
    db.add(contact)
    await db.flush()

    await audit_service.log(
        db, principal=principal,
        action=AuditAction.CONTACT_CREATED,
        target_kind="contact", target_id=contact.id,
        payload={"email": email, "account_id": str(account_id) if account_id else None},
    )
    return contact


def _infer_role_category(title: Optional[str]) -> Optional[str]:
    """从职位推 role_category"""
    if not title:
        return None
    t = title.lower()
    decision_keywords = ("ceo", "cto", "cfo", "coo", "vp", "vice president",
                          "director", "head of", "owner", "founder", "总经理",
                          "总裁", "采购总监")
    if any(k in t for k in decision_keywords):
        return "decision_maker"
    influencer_keywords = ("manager", "lead", "senior", "principal", "经理", "主管")
    if any(k in t for k in influencer_keywords):
        return "influencer"
    gatekeeper_keywords = ("assistant", "secretary", "executive assistant",
                            "秘书", "助理")
    if any(k in t for k in gatekeeper_keywords):
        return "gatekeeper"
    return "user"


async def find_by_email(
    db: AsyncSession, *, tenant_id: uuid.UUID, email: str
) -> Optional[Contact]:
    q = select(Contact).where(
        Contact.tenant_id == tenant_id,
        Contact.email == email.lower().strip(),
    )
    return (await db.execute(q)).scalar_one_or_none()


async def list_contacts(
    db: AsyncSession,
    *,
    principal: Principal,
    tenant_id: uuid.UUID,
    account_id: Optional[uuid.UUID] = None,
    role_category: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Contact]:
    q = select(Contact).where(Contact.tenant_id == tenant_id)
    if account_id:
        q = q.where(Contact.account_id == account_id)
    if role_category:
        q = q.where(Contact.role_category == role_category)
    if search:
        like = f"%{search}%"
        q = q.where(or_(
            Contact.full_name.ilike(like),
            Contact.email.ilike(like),
            Contact.title.ilike(like),
        ))
    q = q.order_by(Contact.created_at.desc()).limit(limit).offset(offset)
    return list((await db.execute(q)).scalars().all())


async def unsubscribe(
    db: AsyncSession,
    *,
    principal: Principal,
    contact_id: uuid.UUID,
    reason: Optional[str] = None,
) -> Contact:
    """退订营销邮件·设 unsubscribed_at + opt_in_marketing=false"""
    contact = await db.get(Contact, contact_id)
    if not contact:
        raise ValueError(f"Contact {contact_id} not found")
    contact.opt_in_marketing = False
    contact.unsubscribed_at = datetime.now(timezone.utc)
    await audit_service.log(
        db, principal=principal,
        action=AuditAction.CONTACT_UNSUBSCRIBED,
        target_kind="contact", target_id=contact_id,
        payload={"reason": reason},
    )
    await db.flush()
    return contact
