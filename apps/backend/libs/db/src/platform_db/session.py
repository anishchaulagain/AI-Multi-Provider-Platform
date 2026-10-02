from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str, *, echo: bool = False, pool_size: int = 10) -> AsyncEngine:
    return create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=pool_size,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
