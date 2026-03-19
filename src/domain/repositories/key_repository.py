from __future__ import annotations

from datetime import datetime
from typing import Protocol

from domain.entities.api_key import ApiKey


class KeyRepository(Protocol):
    async def list_provider_keys(self, provider: str) -> list[ApiKey]:
        ...

    async def list_keys(self, provider: str | None = None) -> list[ApiKey]:
        ...

    async def get_key(self, key_id: str) -> ApiKey | None:
        ...

    async def get_by_provider_credential(
        self, provider: str, credential: dict[str, str]
    ) -> ApiKey | None:
        """Return existing key with same provider and credential, or None."""
        ...

    async def upsert_key(self, key: ApiKey) -> ApiKey:
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

    async def claim_refresh(self, key_id: str, now: datetime, max_age_seconds: int) -> bool:
        """Atomically claim the right to refresh this key. Returns True if claimed.
        Prevents duplicate refresh across multiple processes."""
        ...


class KeyAllocationStore(Protocol):
    async def sync_key(self, key: ApiKey, score: float) -> None:
        ...

    async def remove_key(self, key_id: str, provider: str) -> None:
        ...

    async def allocate_key(
        self,
        provider: str,
        ordered_key_ids: list[str],
        now: datetime,
        lease_seconds: int = 2,
    ) -> str | None:
        ...

    async def allocate_key_any_provider(
        self,
        ordered_keys: list[ApiKey],
        now: datetime,
        lease_seconds: int = 2,
    ) -> str | None:
        ...

    async def release_key_lease(self, provider: str, key_id: str) -> None:
        ...
