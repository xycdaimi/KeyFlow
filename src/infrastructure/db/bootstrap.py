"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-07
@Description: PostgreSQL 写库启动引导
"""
from __future__ import annotations

import asyncio

import asyncpg
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url

from infrastructure.db.models import Base

DB_BOOTSTRAP_MAX_ATTEMPTS = 5
DB_BOOTSTRAP_RETRY_SECONDS = 2
DB_SCHEMA_INIT_MAX_ATTEMPTS = 5
DB_SCHEMA_INIT_RETRY_SECONDS = 2


def build_admin_url(url: str) -> tuple[str, str]:
    parsed = make_url(url)
    database_name = parsed.database
    if not database_name:
        raise ValueError("database URL must include a database name")

    admin_url: URL = parsed.set(
        drivername=parsed.drivername.split("+", 1)[0],
        database="postgres",
    )
    return admin_url.render_as_string(hide_password=False), database_name


async def ensure_database_ready(database_url: str) -> None:
    admin_url, database_name = build_admin_url(database_url)

    for attempt in range(1, DB_BOOTSTRAP_MAX_ATTEMPTS + 1):
        conn = None
        try:
            conn = await asyncpg.connect(admin_url)
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1",
                database_name,
            )
            if exists is None:
                escaped_name = database_name.replace('"', '""')
                await conn.execute(f'CREATE DATABASE "{escaped_name}"')
            return
        except Exception:
            if attempt >= DB_BOOTSTRAP_MAX_ATTEMPTS:
                raise
            await asyncio.sleep(DB_BOOTSTRAP_RETRY_SECONDS)
        finally:
            if conn is not None:
                await conn.close()


async def ensure_schema_ready(write_engine) -> None:
    for attempt in range(1, DB_SCHEMA_INIT_MAX_ATTEMPTS + 1):
        try:
            async with write_engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            return
        except Exception:
            if attempt >= DB_SCHEMA_INIT_MAX_ATTEMPTS:
                raise
            await asyncio.sleep(DB_SCHEMA_INIT_RETRY_SECONDS)


async def ensure_refresh_columns(conn) -> None:
    for col, sql_type in [
        ("last_refreshed_at", "TIMESTAMP WITH TIME ZONE"),
        ("cached_available", "BOOLEAN"),
        ("cached_quota_available", "BOOLEAN"),
        ("cached_capacity_score", "DOUBLE PRECISION"),
    ]:
        await conn.execute(
            text(f"ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS {col} {sql_type}")
        )


async def bootstrap_write_database(database_url: str, write_engine) -> None:
    await ensure_database_ready(database_url)
    await ensure_schema_ready(write_engine)
    async with write_engine.begin() as conn:
        await ensure_refresh_columns(conn)
