"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-06-08
@Description: SQLite 本地运行模式数据库初始化
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from infrastructure.db.models import Base


async def _ensure_sqlite_column(connection, table: str, column: str, definition: str) -> None:
    result = await connection.execute(text(f"PRAGMA table_info({table})"))
    existing = {row[1] for row in result.fetchall()}
    if column not in existing:
        await connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))


async def bootstrap_sqlite_database(sqlite_path: str, write_engine) -> None:
    Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    async with write_engine.begin() as connection:
        await connection.execute(text("PRAGMA journal_mode=WAL"))
        await connection.execute(text("PRAGMA foreign_keys=ON"))
        await connection.execute(text("PRAGMA busy_timeout=30000"))
        await connection.run_sync(Base.metadata.create_all)
        result = await connection.execute(text("PRAGMA table_info(key_leases)"))
        lease_columns = {row[1] for row in result.fetchall()}
        if lease_columns and "lease_id" not in lease_columns:
            await connection.execute(text("DROP TABLE key_leases"))
            await connection.run_sync(Base.metadata.create_all)
        await _ensure_sqlite_column(
            connection,
            "api_keys",
            "max_concurrent_uses",
            "INTEGER DEFAULT 1",
        )
        await connection.execute(
            text("UPDATE api_keys SET max_concurrent_uses = 1 WHERE max_concurrent_uses IS NULL")
        )
        await _ensure_sqlite_column(
            connection,
            "api_keys",
            "consecutive_error_count",
            "INTEGER DEFAULT 0",
        )
        await _ensure_sqlite_column(
            connection,
            "api_keys",
            "cooldown_failure_rounds",
            "INTEGER DEFAULT 0",
        )
        await _ensure_sqlite_column(
            connection,
            "api_keys",
            "rate_limit_rounds",
            "INTEGER DEFAULT 0",
        )
        await _ensure_sqlite_column(
            connection,
            "api_keys",
            "last_report_error_type",
            "VARCHAR(64)",
        )
        await connection.execute(
            text(
                "UPDATE api_keys SET consecutive_error_count = 0 "
                "WHERE consecutive_error_count IS NULL"
            )
        )
        await connection.execute(
            text(
                "UPDATE api_keys SET cooldown_failure_rounds = 0 "
                "WHERE cooldown_failure_rounds IS NULL"
            )
        )
        await connection.execute(
            text("UPDATE api_keys SET rate_limit_rounds = 0 WHERE rate_limit_rounds IS NULL")
        )
