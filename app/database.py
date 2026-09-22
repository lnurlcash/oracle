from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.DATABASE_URL)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    # no migrations, same as timelock-service - fine for this service's
    # own append-mostly schema (events created once, updated once on
    # resolution, nothing else mutates)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        yield session
