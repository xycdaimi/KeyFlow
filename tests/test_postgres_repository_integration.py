"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-03-20
@Description: PostgreSQL 仓储真实集成测试

Real PostgreSQL integration tests for SqlAlchemyKeyRepository.

Uses async SQLAlchemy + asyncpg against a dedicated database. By default
connects to 127.0.0.1:5432; if another PostgreSQL instance already occupies
that port on the host, set KEYFLOW_INTEGRATION_PG_URL (and optionally
KEYFLOW_PG_ADMIN_URL / KEYFLOW_INTEGRATION_PG_BASE) to reach the intended
server (e.g. Docker hostname on a shared Docker network).

Environment:
- KEYFLOW_INTEGRATION_PG_URL: full asyncpg URL for the integration DB (skips auto-create).
- KEYFLOW_PG_ADMIN_URL: sync postgresql:// URL to an existing DB (used to CREATE DATABASE).
- KEYFLOW_INTEGRATION_DB_NAME: database name to create (default keyflow_integration_test).
- KEYFLOW_INTEGRATION_PG_BASE: scheme+auth+host:port without database segment.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from domain.entities.api_key import ApiKey
from domain.exceptions.domain_exceptions import DuplicateCredentialError
from domain.value_objects.key_status import KeyStatus
from infrastructure.db.models import Base
from infrastructure.db.repository_impl import SqlAlchemyKeyRepository

_DEFAULT_ADMIN_URL = "postgresql://keyflow:keyflow@127.0.0.1:5432/keyflow"
_DEFAULT_TEST_DB = "keyflow_integration_test"


async def _ensure_integration_database() -> str:
    """Create dedicated integration DB if missing (connects via admin DB)."""
    admin_url = os.environ.get("KEYFLOW_PG_ADMIN_URL", _DEFAULT_ADMIN_URL)
    db_name = os.environ.get("KEYFLOW_INTEGRATION_DB_NAME", _DEFAULT_TEST_DB)
    if not re.fullmatch(r"[A-Za-z0-9_]+", db_name):
        pytest.fail(f"invalid KEYFLOW_INTEGRATION_DB_NAME: {db_name!r}")
    try:
        conn = await asyncpg.connect(admin_url)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.fail(
            f"PostgreSQL admin connection failed ({admin_url!r}): {exc!r}"
        )
    try:
        row = await conn.fetchrow(
            "SELECT 1 FROM pg_database WHERE datname = $1", db_name
        )
        if row is None:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()
    base = os.environ.get(
        "KEYFLOW_INTEGRATION_PG_BASE",
        "postgresql+asyncpg://keyflow:keyflow@127.0.0.1:5432",
    )
    return f"{base.rstrip('/')}/{db_name}"


@pytest.fixture
async def pg_repository() -> SqlAlchemyKeyRepository:
    test_url = os.environ.get("KEYFLOW_INTEGRATION_PG_URL")
    if not test_url:
        test_url = await _ensure_integration_database()

    engine = create_async_engine(test_url, future=True, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # pragma: no cover - environment-dependent
        await engine.dispose()
        pytest.fail(
            f"PostgreSQL integration DB unavailable ({test_url!r}): {exc!r}"
        )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    repo = SqlAlchemyKeyRepository(factory, factory)
    yield repo

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_upsert_and_get_round_trip_structured_fields(
    pg_repository: SqlAlchemyKeyRepository,
) -> None:
    key_id = f"it-{uuid.uuid4().hex}"
    credential = {
        "api_key": "sk-secret",
        "org_id": "org-42",
        "metadata": '{"tier":"pro","region":"eu"}',
    }
    last_used = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
    cooldown = datetime(2025, 6, 2, 0, 0, tzinfo=timezone.utc)
    refreshed = datetime(2025, 5, 30, 8, 30, tzinfo=timezone.utc)

    original = ApiKey(
        id=key_id,
        provider="openai",
        credential=credential,
        status=KeyStatus.COOLDOWN,
        quota_used=9001,
        last_used_at=last_used,
        success_count=7,
        error_count=3,
        cooldown_until=cooldown,
        supported_models=["gpt-4o", "o1-mini"],
        last_refreshed_at=refreshed,
        cached_available=True,
        cached_quota_available=True,
        cached_capacity_score=0.875,
    )

    saved = await pg_repository.upsert_key(original)
    assert saved == original

    loaded = await pg_repository.get_key(key_id)
    assert loaded is not None
    assert loaded == original

    await pg_repository.delete_key(key_id)


@pytest.mark.asyncio
async def test_runtime_lock_concurrent_only_one_wins(
    pg_repository: SqlAlchemyKeyRepository,
) -> None:
    key_id = f"it-{uuid.uuid4().hex}"
    now = datetime(2025, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
    ttl_seconds = 3600

    await pg_repository.upsert_key(
        ApiKey(
            id=key_id,
            provider="anthropic",
            credential={"api_key": "k"},
            status=KeyStatus.AVAILABLE,
            last_refreshed_at=None,
        )
    )

    async def acquire(owner: str) -> bool:
        return await pg_repository.acquire_runtime_lock(
            key_id,
            owner,
            now,
            ttl_seconds,
            "test",
        )

    a, b = await asyncio.gather(acquire("owner-a"), acquire("owner-b"))
    assert sorted([a, b]) == [False, True]

    await pg_repository.delete_key(key_id)


@pytest.mark.asyncio
async def test_runtime_lock_is_reentrant_for_same_owner(
    pg_repository: SqlAlchemyKeyRepository,
) -> None:
    key_id = f"it-{uuid.uuid4().hex}"
    now = datetime(2025, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
    ttl_seconds = 3600

    await pg_repository.upsert_key(
        ApiKey(
            id=key_id,
            provider="anthropic",
            credential={"api_key": "k"},
            status=KeyStatus.AVAILABLE,
            last_refreshed_at=None,
        )
    )

    first = await pg_repository.acquire_runtime_lock(
        key_id,
        "owner",
        now,
        ttl_seconds,
        "test",
    )
    second = await pg_repository.acquire_runtime_lock(
        key_id,
        "owner",
        now,
        ttl_seconds,
        "test",
    )

    assert first is True
    assert second is True

    await pg_repository.delete_key(key_id)


@pytest.mark.asyncio
async def test_provider_credential_uniqueness_is_enforced(
    pg_repository: SqlAlchemyKeyRepository,
) -> None:
    await pg_repository.upsert_key(
        ApiKey(
            id=f"it-{uuid.uuid4().hex}",
            provider="openai",
            credential={"api_key": "sk-test", "region": "us"},
            status=KeyStatus.AVAILABLE,
        )
    )

    with pytest.raises(DuplicateCredentialError):
        await pg_repository.upsert_key(
            ApiKey(
                id=f"it-{uuid.uuid4().hex}",
                provider="openai",
                credential={"region": "us", "api_key": "sk-test"},
                status=KeyStatus.AVAILABLE,
            )
        )


@pytest.mark.asyncio
async def test_large_oauth_credential_can_be_saved_and_duplicate_is_blocked(
    pg_repository: SqlAlchemyKeyRepository,
) -> None:
    large_credential = {
        "access_token": "a" * 3000,
        "refresh_token": "r" * 3000,
        "scope": "https://www.googleapis.com/auth/cloud-platform",
        "token_type": "Bearer",
        "expiry_date": "1776841403442",
        "type": "gemini_cli_oauth",
        "last_refresh": "2026-04-22T06:03:24.442158+00:00",
    }

    await pg_repository.upsert_key(
        ApiKey(
            id=f"it-{uuid.uuid4().hex}",
            provider="gemini_oauth",
            credential=large_credential,
            status=KeyStatus.AVAILABLE,
        )
    )

    duplicate_credential = {
        "type": "gemini_cli_oauth",
        "refresh_token": "r" * 3000,
        "access_token": "a" * 3000,
        "scope": "https://www.googleapis.com/auth/cloud-platform",
        "token_type": "Bearer",
        "last_refresh": "2026-04-22T06:03:24.442158+00:00",
        "expiry_date": "1776841403442",
    }

    with pytest.raises(DuplicateCredentialError):
        await pg_repository.upsert_key(
            ApiKey(
                id=f"it-{uuid.uuid4().hex}",
                provider="gemini_oauth",
                credential=duplicate_credential,
                status=KeyStatus.AVAILABLE,
            )
        )
