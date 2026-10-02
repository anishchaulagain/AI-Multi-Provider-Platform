"""Idempotently create the default tenant and admin user.

uv run python -m platform_api.cli.seed
"""

import asyncio

from platform_api.core.config import get_settings
from platform_api.core.security import hash_password
from platform_api.repositories import tenants as tenants_repo
from platform_api.repositories import users as users_repo
from platform_db.models import UserRole
from platform_db.session import create_engine, create_sessionmaker


async def seed() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
    try:
        async with sessionmaker() as session:
            tenant = await tenants_repo.get_by_name(session, settings.seed_tenant_name)
            if tenant is None:
                tenant = await tenants_repo.create(session, name=settings.seed_tenant_name)
                print(f"created tenant '{tenant.name}'")

            email = settings.seed_admin_email
            if await users_repo.get_by_email(session, email) is None:
                await users_repo.create(
                    session,
                    tenant_id=tenant.id,
                    email=email,
                    password_hash=hash_password(settings.seed_admin_password.get_secret_value()),
                    full_name="Admin",
                    role=UserRole.ADMIN,
                )
                print(f"created admin user '{email}'")
            else:
                print(f"admin user '{email}' already exists")

            await session.commit()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
