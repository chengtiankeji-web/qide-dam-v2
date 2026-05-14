"""accounts_service · 公司管理 + 智能 dedup + 合并 + AI 背调入口"""
from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal
from app.core.logging import get_logger
from app.models.crm.account import Account
from app.models.crm.contact import Contact
from app.services import audit_service
from app.services.audit_service import AuditAction

logger = get_logger(__name__)


async def create_account(
    db: AsyncSession,
    *,
    principal: Principal,
    tenant_id: uuid.UUID,
    display_name: str,
    legal_name: str | None = None,
    country: str | None = None,
    country_code: str | None = None,
    industry: str | None = None,
    website: str | None = None,
    employee_count: int | None = None,
    annual_revenue_usd: int | None = None,
    primary_email: str | None = None,
    primary_phone: str | None = None,
    source: str | None = None,
    tags: list[str] | None = None,
    dedup_check: bool = True,
) -> Account:
    """创建公司·按 (tenant_id, legal_name 或 website) 去重"""
    if dedup_check:
        existing = await find_matching(
            db, tenant_id=tenant_id,
            legal_name=legal_name, website=website,
        )
        if existing:
            logger.info("account.dedup_hit",
                       account_id=str(existing.id), display_name=display_name)
            return existing

    account = Account(
        tenant_id=tenant_id,
        display_name=display_name,
        legal_name=legal_name,
        country=country,
        country_code=country_code,
        industry=industry,
        website=website,
        employee_count=employee_count,
        annual_revenue_usd=annual_revenue_usd,
        primary_email=primary_email,
        primary_phone=primary_phone,
        source=source,
        tags=tags,
        created_by_user_id=principal.user_id,
    )
    db.add(account)
    await db.flush()

    await audit_service.log(
        db, principal=principal,
        action=AuditAction.ACCOUNT_CREATED,
        target_kind="account", target_id=account.id,
        payload={"display_name": display_name, "country": country},
    )
    return account


async def find_matching(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    legal_name: str | None = None,
    website: str | None = None,
) -> Account | None:
    """按 legal_name OR website 匹配·防重"""
    if not legal_name and not website:
        return None
    conditions = []
    if legal_name:
        conditions.append(Account.legal_name == legal_name.strip())
    if website:
        # normalize website (https/www 处理)
        normalized = website.lower().replace("https://", "").replace("http://", "").replace("www.", "").rstrip("/")
        conditions.append(Account.website.ilike(f"%{normalized}%"))
    q = select(Account).where(
        Account.tenant_id == tenant_id,
        or_(*conditions),
    ).limit(1)
    return (await db.execute(q)).scalar_one_or_none()


async def list_accounts(
    db: AsyncSession,
    *,
    principal: Principal,
    tenant_id: uuid.UUID,
    country: str | None = None,
    industry: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Account]:
    q = select(Account).where(Account.tenant_id == tenant_id)
    if country:
        q = q.where(Account.country == country)
    if industry:
        q = q.where(Account.industry == industry)
    if search:
        like = f"%{search}%"
        q = q.where(or_(
            Account.display_name.ilike(like),
            Account.legal_name.ilike(like),
            Account.website.ilike(like),
        ))
    q = q.order_by(Account.created_at.desc()).limit(limit).offset(offset)
    return list((await db.execute(q)).scalars().all())


async def merge_accounts(
    db: AsyncSession,
    *,
    principal: Principal,
    keep_id: uuid.UUID,
    merge_id: uuid.UUID,
) -> Account:
    """合并两个 account · merge_id 的 contacts/deals 转给 keep_id · merge_id status=merged"""
    keep = await db.get(Account, keep_id)
    merge = await db.get(Account, merge_id)
    if not keep or not merge:
        raise ValueError("Account not found")
    if keep.tenant_id != merge.tenant_id:
        raise ValueError("Cannot merge across tenants")

    # 把 merge 的 contacts 改 account_id
    from sqlalchemy import update as sql_update
    await db.execute(
        sql_update(Contact)
        .where(Contact.account_id == merge_id)
        .values(account_id=keep_id)
    )

    # 把 merge 的 deals / leads 转过来
    from app.models.crm.deal import Deal
    from app.models.crm.lead import Lead
    await db.execute(
        sql_update(Deal).where(Deal.account_id == merge_id).values(account_id=keep_id)
    )
    await db.execute(
        sql_update(Lead).where(Lead.account_id == merge_id).values(account_id=keep_id)
    )

    # 把 merge 标 merged
    merge.status = "merged"

    await audit_service.log(
        db, principal=principal,
        action=AuditAction.ACCOUNT_MERGED,
        target_kind="account", target_id=keep_id,
        payload={"merged_from": str(merge_id),
                 "merged_display_name": merge.display_name},
    )
    await db.flush()
    return keep
