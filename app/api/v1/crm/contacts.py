"""/v1/crm/contacts · 联系人 REST API"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, require_authenticated
from app.db.session import get_db
from app.schemas.crm.contact import (
    ContactCreate, ContactOut, ContactListOut, ContactUpdate
)
from app.services.crm import contacts_service

router = APIRouter()


@router.post("/", response_model=ContactOut, status_code=http_status.HTTP_201_CREATED)
async def create_contact(
    payload: ContactCreate,
    principal: Principal = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> ContactOut:
    contact = await contacts_service.create_contact(
        db,
        principal=principal,
        tenant_id=principal.tenant_id,
        full_name=payload.full_name,
        email=payload.email,
        phone=payload.phone,
        title=payload.title,
        role_category=payload.role_category,
        account_id=payload.account_id,
        linkedin_url=payload.linkedin_url,
        source=payload.source,
    )
    await db.commit()
    return ContactOut.model_validate(contact)


@router.get("/", response_model=ContactListOut)
async def list_contacts(
    account_id: Optional[uuid.UUID] = Query(None),
    role_category: Optional[str] = Query(None),
    search: Optional[str] = Query(None, max_length=128),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> ContactListOut:
    rows = await contacts_service.list_contacts(
        db,
        principal=principal,
        tenant_id=principal.tenant_id,
        account_id=account_id,
        role_category=role_category,
        search=search,
        limit=limit,
        offset=offset,
    )
    return ContactListOut(
        items=[ContactOut.model_validate(r) for r in rows],
        total=len(rows), limit=limit, offset=offset,
    )


@router.get("/{contact_id}", response_model=ContactOut)
async def get_contact(
    contact_id: uuid.UUID,
    principal: Principal = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> ContactOut:
    from app.models.crm.contact import Contact
    contact = await db.get(Contact, contact_id)
    if not contact:
        raise HTTPException(404, "Contact not found")
    if not principal.is_platform_admin and contact.tenant_id != principal.tenant_id:
        raise HTTPException(403, "Forbidden")
    return ContactOut.model_validate(contact)


@router.post("/{contact_id}/unsubscribe", response_model=ContactOut)
async def unsubscribe_contact(
    contact_id: uuid.UUID,
    reason: Optional[str] = Query(None, max_length=256),
    principal: Principal = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> ContactOut:
    contact = await contacts_service.unsubscribe(
        db, principal=principal, contact_id=contact_id, reason=reason,
    )
    await db.commit()
    return ContactOut.model_validate(contact)
