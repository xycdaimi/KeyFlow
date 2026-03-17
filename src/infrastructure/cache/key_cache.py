from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from redis.asyncio import Redis

from domain.entities.api_key import ApiKey
from domain.repositories.key_repository import KeyAllocationStore


def to_epoch(value: datetime | None) -> str:
    if value is None:
        return ""
    return str(int(value.astimezone(timezone.utc).timestamp()))


class RedisKeyCache(KeyAllocationStore):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._script = Path(__file__).with_name("lua").joinpath("allocate.lua").read_text(encoding="utf-8")

    def _provider_zset(self, provider: str) -> str:
        return f"keyflow:provider:{provider}:keys"

    def _key_hash(self, key_id: str) -> str:
        return f"keyflow:key:{key_id}"

    async def sync_key(self, key: ApiKey, score: float) -> None:
        zset_key = self._provider_zset(key.provider)
        hash_key = self._key_hash(key.id)

        await self._redis.zadd(zset_key, {key.id: score})
        await self._redis.hset(
            hash_key,
            mapping={
                "provider": key.provider,
                "status": key.status.value,
                "cooldown_until": to_epoch(key.cooldown_until),
                "last_used_at": to_epoch(key.last_used_at),
            },
        )

    async def remove_key(self, key_id: str, provider: str) -> None:
        await self._redis.zrem(self._provider_zset(provider), key_id)
        await self._redis.delete(self._key_hash(key_id))

    async def allocate_key(self, provider: str, ordered_key_ids: list[str], now: datetime) -> str | None:
        if not ordered_key_ids:
            return None
        result = await self._redis.eval(
            self._script,
            1,
            self._provider_zset(provider),
            str(int(now.astimezone(timezone.utc).timestamp())),
            *ordered_key_ids,
        )
        return result if isinstance(result, str) and result else None
