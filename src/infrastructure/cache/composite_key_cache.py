"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-06-05
@Description: Redis 优先、数据库兜底的 Key 原子分配租约
"""
from __future__ import annotations

import logging
from datetime import datetime

from domain.entities.api_key import ApiKey
from domain.exceptions.domain_exceptions import AllocationStoreUnavailableError
from domain.repositories.key_repository import AllocationLease, KeyAllocationStore
from domain.value_objects.key_pool import KeyPool

logger = logging.getLogger(__name__)


def _lease_kwargs(lease_id: str | None) -> dict[str, str]:
    return {} if lease_id is None else {"lease_id": lease_id}


class CompositeKeyCache(KeyAllocationStore):
    def __init__(self, primary: KeyAllocationStore, fallback: KeyAllocationStore) -> None:
        self._primary = primary
        self._fallback = fallback

    async def sync_key(self, key: ApiKey, score: float) -> None:
        fallback_error: Exception | None = None
        try:
            await self._fallback.sync_key(key, score)
        except Exception as exc:
            fallback_error = exc
            logger.warning("fallback allocation sync failed for %s: %s", key.id, exc)
        try:
            await self._primary.sync_key(key, score)
        except Exception as exc:
            logger.warning("primary allocation sync failed for %s: %s", key.id, exc)
            if fallback_error is not None:
                raise AllocationStoreUnavailableError("all allocation stores failed to sync") from exc

    async def remove_key(self, key_id: str, provider: str, pool: KeyPool) -> None:
        primary_error: Exception | None = None
        try:
            await self._primary.remove_key(key_id, provider, pool)
        except Exception as exc:
            primary_error = exc
            logger.warning("primary allocation remove failed for %s: %s", key_id, exc)
        try:
            await self._fallback.remove_key(key_id, provider, pool)
        except Exception as exc:
            if primary_error is not None:
                raise AllocationStoreUnavailableError("all allocation stores failed to remove key") from exc
            raise

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
        try:
            lease = await self._primary.allocate_key(
                provider,
                pool,
                ordered_key_ids,
                now,
                lease_seconds=lease_seconds,
                allow_leased_fallback=allow_leased_fallback,
                **_lease_kwargs(lease_id),
            )
            if lease is not None:
                try:
                    fallback_lease = await self._fallback.allocate_key(
                        provider,
                        pool,
                        [lease.key_id],
                        now,
                        lease_seconds=lease_seconds,
                        allow_leased_fallback=False,
                        **_lease_kwargs(lease.lease_id),
                    )
                except Exception as exc:
                    logger.warning("fallback allocation mirror failed for %s: %s", lease.key_id, exc)
                return lease
            return None
        except Exception as exc:
            logger.warning("primary allocation failed, using fallback: %s", exc)
        try:
            return await self._fallback.allocate_key(
                provider,
                pool,
                ordered_key_ids,
                now,
                lease_seconds=lease_seconds,
                allow_leased_fallback=allow_leased_fallback,
                **_lease_kwargs(lease_id),
            )
        except Exception as exc:
            raise AllocationStoreUnavailableError("fallback allocation store failed") from exc

    async def allocate_key_any_provider(
        self,
        pool: KeyPool,
        ordered_keys: list[ApiKey],
        now: datetime,
        lease_seconds: int = 2,
        allow_leased_fallback: bool = True,
        lease_id: str | None = None,
    ) -> AllocationLease | None:
        try:
            lease = await self._primary.allocate_key_any_provider(
                pool,
                ordered_keys,
                now,
                lease_seconds=lease_seconds,
                allow_leased_fallback=allow_leased_fallback,
                **_lease_kwargs(lease_id),
            )
            if lease is not None:
                provider = self._provider_for_key(ordered_keys, lease.key_id)
                if provider is not None:
                    try:
                        fallback_lease = await self._fallback.allocate_key(
                            provider,
                            pool,
                            [lease.key_id],
                            now,
                            lease_seconds=lease_seconds,
                            allow_leased_fallback=False,
                            **_lease_kwargs(lease.lease_id),
                        )
                    except Exception as exc:
                        logger.warning(
                            "fallback model allocation mirror failed for %s: %s",
                            lease.key_id,
                            exc,
                        )
                return lease
            return None
        except Exception as exc:
            logger.warning("primary model allocation failed, using fallback: %s", exc)
        try:
            return await self._fallback.allocate_key_any_provider(
                pool,
                ordered_keys,
                now,
                lease_seconds=lease_seconds,
                allow_leased_fallback=allow_leased_fallback,
                **_lease_kwargs(lease_id),
            )
        except Exception as exc:
            raise AllocationStoreUnavailableError("fallback model allocation store failed") from exc

    async def release_key_lease(self, provider: str, pool: KeyPool, key_id: str, lease_id: str) -> None:
        primary_error: Exception | None = None
        try:
            await self._primary.release_key_lease(provider, pool, key_id, lease_id)
        except Exception as exc:
            primary_error = exc
            logger.warning("primary lease release failed for %s: %s", key_id, exc)
        try:
            await self._fallback.release_key_lease(provider, pool, key_id, lease_id)
        except Exception as exc:
            if primary_error is not None:
                raise AllocationStoreUnavailableError("all allocation stores failed to release lease") from exc
            raise

    @staticmethod
    def _provider_for_key(keys: list[ApiKey], key_id: str) -> str | None:
        for key in keys:
            if key.id == key_id:
                return key.provider
        return None
