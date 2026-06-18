"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-06-05
@Description: Redis Key 分配缓存与短租约
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from redis.asyncio import Redis

from domain.entities.api_key import ApiKey
from domain.repositories.key_repository import AllocationLease, KeyAllocationStore
from domain.value_objects.key_pool import KeyPool


def to_epoch(value: datetime | None) -> str:
    if value is None:
        return ""
    return str(int(value.astimezone(timezone.utc).timestamp()))


class RedisKeyCache(KeyAllocationStore):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._script = Path(__file__).with_name("lua").joinpath("allocate.lua").read_text(encoding="utf-8")
        self._release_script = Path(__file__).with_name("lua").joinpath("release.lua").read_text(encoding="utf-8")

    def _provider_zset(self, provider: str, pool: KeyPool) -> str:
        return f"keyflow:provider:{provider}:pool:{pool.value}:keys"

    def _key_hash(self, key_id: str) -> str:
        return f"keyflow:key:{key_id}"

    def _key_lease_zset(self, key_id: str) -> str:
        return f"keyflow:key:{key_id}:leases"

    async def sync_key(self, key: ApiKey, score: float) -> None:
        zset_key = self._provider_zset(key.provider, key.pool)
        hash_key = self._key_hash(key.id)

        await self._redis.zadd(zset_key, {key.id: score})
        await self._redis.hset(
            hash_key,
            mapping={
                "provider": key.provider,
                "status": key.status.value,
                "cooldown_until": to_epoch(key.cooldown_until),
                "last_used_at": to_epoch(key.last_used_at),
                "max_concurrent_uses": str(max(key.max_concurrent_uses, 1)),
            },
        )

    async def remove_key(self, key_id: str, provider: str, pool: KeyPool) -> None:
        lease_zset = self._key_lease_zset(key_id)
        lease_ids = await self._redis.zrange(lease_zset, 0, -1)
        if lease_ids:
            await self._redis.delete(*(f"keyflow:lease:{lease_id}" for lease_id in lease_ids))
        await self._redis.zrem(self._provider_zset(provider, pool), key_id)
        await self._redis.delete(self._key_hash(key_id), lease_zset)

    async def allocate_key(
        self,
        provider: str,
        pool: KeyPool,
        ordered_key_ids: list[str],
        now: datetime,
        lease_seconds: int = 2,
        allow_leased_fallback: bool = True,
        lease_id: str | None = None,
    ) -> AllocationLease | None:
        if not ordered_key_ids:
            return None
        lease_id = lease_id or uuid4().hex
        result = await self._redis.eval(
            self._script,
            1,
            self._provider_zset(provider, pool),
            str(int(now.astimezone(timezone.utc).timestamp())),
            str(max(lease_seconds, 1)),
            provider,
            pool.value,
            lease_id,
            *ordered_key_ids,
        )
        return AllocationLease(key_id=result, lease_id=lease_id) if isinstance(result, str) and result else None

    async def allocate_key_any_provider(
        self,
        pool: KeyPool,
        ordered_keys: list[ApiKey],
        now: datetime,
        lease_seconds: int = 2,
        allow_leased_fallback: bool = True,
        lease_id: str | None = None,
    ) -> AllocationLease | None:
        for key in ordered_keys:
            lease = await self.allocate_key(
                key.provider,
                pool,
                [key.id],
                now,
                lease_seconds=lease_seconds,
                allow_leased_fallback=False,
                lease_id=lease_id,
            )
            if lease is not None:
                return lease
        if not allow_leased_fallback:
            return None
        for key in ordered_keys:
            lease = await self.allocate_key(
                key.provider,
                pool,
                [key.id],
                now,
                lease_seconds=lease_seconds,
                allow_leased_fallback=True,
                lease_id=lease_id,
            )
            if lease is not None:
                return lease
        return None

    async def release_key_lease(
        self,
        provider: str,
        pool: KeyPool,
        key_id: str,
        lease_id: str | None = None,
    ) -> None:
        if lease_id is None:
            lease_zset = self._key_lease_zset(key_id)
            lease_ids = await self._redis.zrange(lease_zset, 0, -1)
            if lease_ids:
                await self._redis.delete(*(f"keyflow:lease:{item}" for item in lease_ids))
            await self._redis.delete(lease_zset)
            return
        await self._redis.eval(
            self._release_script,
            0,
            provider,
            pool.value,
            key_id,
            lease_id,
        )
