"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-13
@Description: SQLite 本地运行模式会话工厂
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def _sqlite_url(sqlite_path: str) -> str:
    path = Path(sqlite_path)
    if not path.is_absolute():
        path = path.resolve()
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def create_sqlite_session_factory(
    sqlite_path: str,
) -> tuple[async_sessionmaker[AsyncSession], async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        _sqlite_url(sqlite_path),
        connect_args={"timeout": 30},
        pool_pre_ping=True,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    return factory, factory
