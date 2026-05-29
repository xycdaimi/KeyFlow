"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-29
@Description: Key 仓储协议定义
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from domain.entities.api_key import ApiKey
from domain.value_objects.key_pool import KeyPool


class KeyRepository(Protocol):
    async def list_provider_keys(self, provider: str) -> list[ApiKey]:
        ...

    async def list_provider_pool_keys(self, provider: str, pool: KeyPool) -> list[ApiKey]:
        ...

    async def list_keys(self, provider: str | None = None) -> list[ApiKey]:
        ...

    async def list_pool_keys(self, pool: KeyPool) -> list[ApiKey]:
        ...

    async def get_key(self, key_id: str) -> ApiKey | None:
        ...

    async def get_by_provider_credential(
        self, provider: str, credential: dict[str, Any]
    ) -> ApiKey | None:
        """Return existing key with same provider and credential, or None."""
        ...

    async def upsert_key(self, key: ApiKey) -> ApiKey:
        ...

    async def touch_key_used(self, key_id: str, now: datetime) -> ApiKey | None:
        """Update allocation usage timestamp only."""
        ...

    async def record_success(self, key_id: str, tokens_used: int, now: datetime) -> ApiKey | None:
        """Increment success priority fields without changing runtime/admin state."""
        ...

    async def record_error(self, key_id: str, now: datetime) -> ApiKey | None:
        """Increment error priority fields without changing runtime/admin state."""
        ...

    async def update_status(
        self,
        key_id: str,
        status: str,
        cooldown_until: datetime | None,
        now: datetime,
    ) -> ApiKey | None:
        """Update status-owned fields only."""
        ...

    async def update_pool(self, key_id: str, pool: KeyPool) -> ApiKey | None:
        """Update pool membership without resetting runtime counters."""
        ...

    async def update_max_concurrent_uses(
        self, key_id: str, max_concurrent_uses: int
    ) -> ApiKey | None:
        """Update credential-level active allocation limit."""
        ...

    async def acquire_runtime_lock(
        self,
        key_id: str,
        owner: str,
        now: datetime,
        ttl_seconds: int,
        reason: str,
    ) -> bool:
        """Acquire per-key runtime snapshot write lock."""
        ...

    async def release_runtime_lock(self, key_id: str, owner: str, now: datetime) -> None:
        """Release per-key runtime snapshot write lock when still owned by owner."""
        ...

    async def update_runtime_snapshot_if_locked(
        self,
        key: ApiKey,
        owner: str,
        now: datetime,
    ) -> ApiKey | None:
        """Persist explicit runtime mutation when owner still holds the lock."""
        ...

    async def update_background_runtime_snapshot_if_locked(
        self,
        key: ApiKey,
        owner: str,
        now: datetime,
    ) -> ApiKey | None:
        """Persist background runtime refresh only if owner holds the lock and key is not admin/report disabled."""
        ...

    async def delete_key(self, key_id: str) -> None:
        ...

    async def list_recoverable_keys(self, now: datetime) -> list[ApiKey]:
        ...

    async def list_keys_needing_refresh(
        self, cutoff: datetime, provider: str | None = None
    ) -> list[ApiKey]:
        """List keys whose last_refreshed_at is None or older than cutoff."""
        ...


class KeyAllocationStore(Protocol):
    async def sync_key(self, key: ApiKey, score: float) -> None:
        ...

    async def remove_key(self, key_id: str, provider: str, pool: KeyPool) -> None:
        ...

    async def allocate_key(
        self,
        provider: str,
        pool: KeyPool,
        ordered_key_ids: list[str],
        now: datetime,
        lease_seconds: int = 2,
        allow_leased_fallback: bool = True,
    ) -> str | None:
        ...

    async def allocate_key_any_provider(
        self,
        pool: KeyPool,
        ordered_keys: list[ApiKey],
        now: datetime,
        lease_seconds: int = 2,
        allow_leased_fallback: bool = True,
    ) -> str | None:
        ...

    async def release_key_lease(self, provider: str, pool: KeyPool, key_id: str) -> None:
        ...
