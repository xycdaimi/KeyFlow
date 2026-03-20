"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-03-20
@Description: Redis Lua 分配与租约真实集成测试
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
from redis.asyncio import Redis

from domain.entities.api_key import ApiKey
from domain.value_objects.key_status import KeyStatus
from infrastructure.cache.key_cache import RedisKeyCache

REDIS_URL = os.environ.get("KEYFLOW_INTEGRATION_REDIS_URL", "redis://localhost:6379/9")
_CONCURRENT_ATTEMPTS = 64
_SHORT_LEASE_SECONDS = 1
_LEASE_EXPIRY_WAIT_SECONDS = 1.5
_LONG_LEASE_SECONDS = 30


@pytest.fixture
async def redis_client() -> Redis:
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - environment-dependent
        await client.aclose()
        pytest.fail(f"Redis at {REDIS_URL} unavailable: {exc!r}")
    yield client
    await client.aclose()


@pytest.fixture
async def isolated_cache(redis_client: Redis):
    """One provider + key id per test; cleans leases, zset member, and hash after."""
    provider = f"it-{uuid.uuid4().hex[:16]}"
    key_id = f"k-{uuid.uuid4().hex[:16]}"
    cache = RedisKeyCache(redis_client)
    yield cache, provider, key_id
    await cache.release_key_lease(provider, key_id)
    await cache.remove_key(key_id, provider)


@pytest.mark.asyncio
async def test_parallel_allocate_same_key_at_most_once_while_lease_active(
    isolated_cache: tuple[RedisKeyCache, str, str],
) -> None:
    cache, provider, key_id = isolated_cache
    await cache.sync_key(
        ApiKey(id=key_id, provider=provider, credential={}, status=KeyStatus.AVAILABLE),
        score=0.0,
    )

    async def try_allocate() -> str | None:
        return await cache.allocate_key(
            provider,
            [key_id],
            datetime.now(timezone.utc),
            lease_seconds=_LONG_LEASE_SECONDS,
        )

    results = await asyncio.gather(*[try_allocate() for _ in range(_CONCURRENT_ATTEMPTS)])
    winner_count = results.count(key_id)

    assert winner_count == 1, f"expected exactly one allocation of {key_id!r}, got {results!r}"
    assert all(result in {None, key_id} for result in results)


@pytest.mark.asyncio
async def test_expired_lease_allows_re_allocation(isolated_cache: tuple[RedisKeyCache, str, str]) -> None:
    cache, provider, key_id = isolated_cache
    await cache.sync_key(
        ApiKey(id=key_id, provider=provider, credential={}, status=KeyStatus.AVAILABLE),
        score=0.0,
    )

    first = await cache.allocate_key(
        provider,
        [key_id],
        datetime.now(timezone.utc),
        lease_seconds=_SHORT_LEASE_SECONDS,
    )
    assert first == key_id

    await asyncio.sleep(_LEASE_EXPIRY_WAIT_SECONDS)

    second = await cache.allocate_key(
        provider,
        [key_id],
        datetime.now(timezone.utc),
        lease_seconds=_LONG_LEASE_SECONDS,
    )
    assert second == key_id


@pytest.mark.asyncio
async def test_released_lease_allows_immediate_re_allocation(
    isolated_cache: tuple[RedisKeyCache, str, str],
) -> None:
    cache, provider, key_id = isolated_cache
    await cache.sync_key(
        ApiKey(id=key_id, provider=provider, credential={}, status=KeyStatus.AVAILABLE),
        score=0.0,
    )

    first = await cache.allocate_key(
        provider,
        [key_id],
        datetime.now(timezone.utc),
        lease_seconds=_LONG_LEASE_SECONDS,
    )
    assert first == key_id

    await cache.release_key_lease(provider, key_id)

    second = await cache.allocate_key(
        provider,
        [key_id],
        datetime.now(timezone.utc),
        lease_seconds=_LONG_LEASE_SECONDS,
    )
    assert second == key_id
