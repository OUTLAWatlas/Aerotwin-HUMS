"""
Async SQLAlchemy engine/session management. Stateless: any API replica can
use the same connection pool settings against the shared TimescaleDB.
"""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()


@lru_cache
def get_engine():
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        future=True,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=get_engine(), expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncSession:
    """FastAPI dependency yielding a request-scoped session."""
    Session = get_sessionmaker()
    async with Session() as session:
        yield session
