"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-13
@Description: SQLite 本地运行模式数据库初始化
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from infrastructure.db.models import Base


async def bootstrap_sqlite_database(sqlite_path: str, write_engine) -> None:
    Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    async with write_engine.begin() as connection:
        await connection.execute(text("PRAGMA journal_mode=WAL"))
        await connection.execute(text("PRAGMA foreign_keys=ON"))
        await connection.execute(text("PRAGMA busy_timeout=30000"))
        await connection.run_sync(Base.metadata.create_all)
