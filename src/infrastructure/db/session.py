from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def create_session_factory(
    database_read_url: str,
    database_write_url: str,
) -> tuple[async_sessionmaker[AsyncSession], async_sessionmaker[AsyncSession]]:
    read_engine = create_async_engine(database_read_url, future=True, pool_pre_ping=True)
    write_engine = create_async_engine(database_write_url, future=True, pool_pre_ping=True)
    read_factory = async_sessionmaker(read_engine, expire_on_commit=False)
    write_factory = async_sessionmaker(write_engine, expire_on_commit=False)
    return read_factory, write_factory
