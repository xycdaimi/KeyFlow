"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-06-08
@Description: PostgreSQL 写库启动引导
"""
from __future__ import annotations

import asyncio

import asyncpg
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url

from infrastructure.db.models import Base
from infrastructure.db.repository_impl import credential_fingerprint

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
                if await ensure_task_lease_table(connection):
                    await connection.run_sync(Base.metadata.create_all)
            return
        except Exception:
            if attempt >= DB_SCHEMA_INIT_MAX_ATTEMPTS:
                raise
            await asyncio.sleep(DB_SCHEMA_INIT_RETRY_SECONDS)


async def ensure_refresh_columns(conn) -> None:
    for col, sql_type in [
        ("last_refreshed_at", "TIMESTAMP WITH TIME ZONE"),
        ("updated_at", "TIMESTAMP WITH TIME ZONE"),
        ("cached_available", "BOOLEAN"),
        ("cached_quota_available", "BOOLEAN"),
        ("cached_capacity_score", "DOUBLE PRECISION"),
        ("runtime_lock_owner", "VARCHAR(128)"),
        ("runtime_lock_until", "TIMESTAMP WITH TIME ZONE"),
        ("runtime_lock_reason", "VARCHAR(64)"),
        ("credential_fingerprint", "VARCHAR(64)"),
        ("pool", "VARCHAR(32) DEFAULT 'default'"),
        ("max_concurrent_uses", "INTEGER DEFAULT 1"),
        ("consecutive_error_count", "INTEGER DEFAULT 0"),
        ("cooldown_failure_rounds", "INTEGER DEFAULT 0"),
        ("rate_limit_rounds", "INTEGER DEFAULT 0"),
        ("last_report_error_type", "VARCHAR(64)"),
    ]:
        await conn.execute(
            text(f"ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS {col} {sql_type}")
        )


async def ensure_task_lease_table(conn) -> bool:
    if not hasattr(conn, "execute"):
        return False
    result = await conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'key_leases'"
        )
    )
    columns = {row[0] for row in result.fetchall()}
    if columns and "lease_id" not in columns:
        await conn.execute(text("DROP TABLE key_leases"))
        return True
    return False


async def ensure_credential_uniqueness(conn) -> None:
    await conn.execute(text("DROP INDEX IF EXISTS uq_api_keys_provider_credential"))
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_api_keys_provider_credential "
            "ON api_keys (provider, credential_fingerprint)"
        )
    )


async def backfill_credential_fingerprints(conn) -> None:
    result = await conn.execute(text("SELECT id, credential FROM api_keys"))
    rows = result.mappings().all()
    for row in rows:
        await conn.execute(
            text(
                "UPDATE api_keys SET credential_fingerprint = :fingerprint "
                "WHERE id = :id AND credential_fingerprint IS NULL"
            ),
            {
                "id": row["id"],
                "fingerprint": credential_fingerprint(row["credential"]),
            },
        )


async def backfill_key_pools(conn) -> None:
    await conn.execute(text("UPDATE api_keys SET pool = 'default' WHERE pool IS NULL"))


async def backfill_concurrency_limits(conn) -> None:
    await conn.execute(
        text("UPDATE api_keys SET max_concurrent_uses = 1 WHERE max_concurrent_uses IS NULL")
    )


async def backfill_report_state_columns(conn) -> None:
    await conn.execute(
        text(
            "UPDATE api_keys SET consecutive_error_count = 0 "
            "WHERE consecutive_error_count IS NULL"
        )
    )
    await conn.execute(
        text(
            "UPDATE api_keys SET cooldown_failure_rounds = 0 "
            "WHERE cooldown_failure_rounds IS NULL"
        )
    )
    await conn.execute(
        text("UPDATE api_keys SET rate_limit_rounds = 0 WHERE rate_limit_rounds IS NULL")
    )


async def ensure_credential_fingerprint_not_null(conn) -> None:
    await conn.execute(
        text("ALTER TABLE api_keys ALTER COLUMN credential_fingerprint SET NOT NULL")
    )


async def bootstrap_write_database(database_url: str, write_engine) -> None:
    await ensure_database_ready(database_url)
    await ensure_schema_ready(write_engine)
    async with write_engine.begin() as conn:
        await ensure_refresh_columns(conn)
        await ensure_task_lease_table(conn)
        await backfill_key_pools(conn)
        await backfill_concurrency_limits(conn)
        await backfill_report_state_columns(conn)
        await backfill_credential_fingerprints(conn)
        await ensure_credential_fingerprint_not_null(conn)
        await ensure_credential_uniqueness(conn)
