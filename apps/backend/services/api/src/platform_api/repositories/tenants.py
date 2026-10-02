from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_db.models import Tenant


async def get_by_name(session: AsyncSession, name: str) -> Tenant | None:
    result = await session.execute(select(Tenant).where(Tenant.name == name))
    return result.scalar_one_or_none()


async def create(session: AsyncSession, *, name: str) -> Tenant:
    tenant = Tenant(name=name)
    session.add(tenant)
    await session.flush()
    return tenant
