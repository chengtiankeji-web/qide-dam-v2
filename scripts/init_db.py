"""Bootstrap script: create the platform admin user.

Run after `alembic upgrade head`. Idempotent.

Usage:
    python -m scripts.init_db --email admin@qide.com --password 'CHANGE_ME' \\
                              --tenant-slug qide
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.models.tenant import Tenant
from app.models.user import User


async def run(email: str, password: str, tenant_slug: str, full_name: str) -> None:
    async with AsyncSessionLocal() as db:  # type: AsyncSession
        tenant = (
            await db.execute(select(Tenant).where(Tenant.slug == tenant_slug))
        ).scalar_one_or_none()
        if not tenant:
            print(f"ERROR: tenant '{tenant_slug}' not found. Run alembic first.", file=sys.stderr)
            sys.exit(1)

        existing = (
            await db.execute(
                select(User).where(User.email == email, User.tenant_id == tenant.id)
            )
        ).scalar_one_or_none()
        if existing:
            print(f"User {email} already exists in tenant {tenant_slug}; flipping to platform admin.")
            existing.is_platform_admin = True
            existing.role = "platform_admin"
            existing.is_active = True
            existing.password_hash = hash_password(password)
            existing.project_access = ["*"]
        else:
            db.add(
                User(
                    tenant_id=tenant.id,
                    email=email,
                    full_name=full_name,
                    password_hash=hash_password(password),
                    role="platform_admin",
                    is_platform_admin=True,
                    project_access=["*"],
                )
            )
        await db.commit()
        print(f"OK: platform admin {email} ready in tenant {tenant_slug}.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--tenant-slug", default="qide")
    parser.add_argument("--full-name", default="Platform Admin")
    args = parser.parse_args()
    asyncio.run(run(args.email, args.password, args.tenant_slug, args.full_name))


if __name__ == "__main__":
    main()
