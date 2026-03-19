from __future__ import annotations

import json
from datetime import datetime, timedelta

from domain.entities.api_key import ApiKey
from infrastructure.plugins.base import CapacitySignal, ProviderPlugin, ProviderRegistry


class InMemoryKeyRepository:
    def __init__(self, keys: list[ApiKey] | None = None) -> None:
        self._keys = {key.id: key for key in keys or []}

    async def list_provider_keys(self, provider: str) -> list[ApiKey]:
        return [key for key in self._keys.values() if key.provider == provider]

    async def list_keys(self, provider: str | None = None) -> list[ApiKey]:
        if provider is None:
            return list(self._keys.values())
        return [key for key in self._keys.values() if key.provider == provider]

    async def get_key(self, key_id: str) -> ApiKey | None:
        return self._keys.get(key_id)

    async def get_by_provider_credential(
        self, provider: str, credential: dict[str, str]
    ) -> ApiKey | None:
        for key in self._keys.values():
            if key.provider == provider and json.dumps(key.credential, sort_keys=True) == json.dumps(
                credential, sort_keys=True
            ):
                return key
        return None

    async def upsert_key(self, key: ApiKey) -> ApiKey:
        self._keys[key.id] = key
        return key

    async def delete_key(self, key_id: str) -> None:
        self._keys.pop(key_id, None)

    async def list_recoverable_keys(self, now: datetime) -> list[ApiKey]:
        return [
            key
            for key in self._keys.values()
            if key.cooldown_until is not None and key.cooldown_until <= now
        ]

    async def list_keys_needing_refresh(
        self, cutoff: datetime, provider: str | None = None
    ) -> list[ApiKey]:
        keys = [
            key
            for key in self._keys.values()
            if key.last_refreshed_at is None or key.last_refreshed_at < cutoff
        ]
        if provider:
            keys = [k for k in keys if k.provider == provider]
        return keys

    async def claim_refresh(self, key_id: str, now: datetime, max_age_seconds: int) -> bool:
        from datetime import timedelta

        key = self._keys.get(key_id)
        if key is None:
            return False
        cutoff = now - timedelta(seconds=max_age_seconds)
        if key.last_refreshed_at is not None and key.last_refreshed_at >= cutoff:
            return False
        key.last_refreshed_at = now
        return True


class InMemoryAllocationStore:
    def __init__(self) -> None:
        self.synced_scores: dict[str, float] = {}
        self.released: list[tuple[str, str]] = []
        self.allocate_calls: list[tuple[str, list[str]]] = []
        self.any_provider_calls: list[dict[str, object]] = []
        self.any_provider_ordered_ids: list[str] = []
        self.removed_keys: set[tuple[str, str]] = set()
        self.active_leases: dict[tuple[str, str], datetime] = {}

    async def sync_key(self, key: ApiKey, score: float) -> None:
        self.synced_scores[key.id] = score
        self.removed_keys.discard((key.provider, key.id))

    async def remove_key(self, key_id: str, provider: str) -> None:
        self.synced_scores.pop(key_id, None)
        self.removed_keys.add((provider, key_id))
        self.active_leases.pop((provider, key_id), None)

    def _prune_expired_leases(self, now: datetime) -> None:
        expired = [lease_key for lease_key, expires_at in self.active_leases.items() if expires_at <= now]
        for lease_key in expired:
            self.active_leases.pop(lease_key, None)

    async def allocate_key(
        self,
        provider: str,
        ordered_key_ids: list[str],
        now: datetime,
        lease_seconds: int = 2,
    ) -> str | None:
        self.allocate_calls.append((provider, list(ordered_key_ids)))
        self._prune_expired_leases(now)
        for key_id in ordered_key_ids:
            lease_key = (provider, key_id)
            if lease_key in self.removed_keys or lease_key in self.active_leases:
                continue
            self.active_leases[lease_key] = now + timedelta(seconds=max(lease_seconds, 1))
            return key_id
        return None

    async def allocate_key_any_provider(
        self,
        ordered_keys: list[ApiKey],
        now: datetime,
        lease_seconds: int = 2,
    ) -> str | None:
        self.any_provider_calls.append(
            {
                "ordered_candidates": [(key.provider, key.id) for key in ordered_keys],
                "lease_seconds": lease_seconds,
            }
        )
        self.any_provider_ordered_ids = [key.id for key in ordered_keys]
        for key in ordered_keys:
            allocated_id = await self.allocate_key(
                key.provider,
                [key.id],
                now,
                lease_seconds=lease_seconds,
            )
            if allocated_id is not None:
                return allocated_id
        return None

    async def release_key_lease(self, provider: str, key_id: str) -> None:
        self.released.append((provider, key_id))
        self.active_leases.pop((provider, key_id), None)


class InMemoryAnyProviderAllocationStore(InMemoryAllocationStore):
    pass


class FakeProviderPlugin(ProviderPlugin):
    """In-memory plugin for unit tests.

    ``available`` controls the return value of is_credential_available.
    """

    def __init__(
        self,
        name: str,
        models: list[str] | None = None,
        available: bool = True,
        capacity_signal: CapacitySignal | None = None,
        capacity_by_credential: dict[tuple[tuple[str, str], ...], CapacitySignal | None] | None = None,
    ) -> None:
        self._name = name
        self._models = models or []
        self._available = available
        self._capacity_signal = capacity_signal
        self._capacity_by_credential = capacity_by_credential or {}
        self.success_calls: list[tuple[dict[str, str], dict]] = []
        self.error_calls: list[tuple[dict[str, str], dict]] = []
        self.available_checks: list[tuple[dict[str, str], str | None]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Fake plugin for {self._name}"

    @property
    def auth_type(self) -> str:
        return "bearer_api_key"

    @property
    def credential_hint(self) -> str:
        return '{"api_key": "sk-***"}'

    async def fetch_models(self, credential: dict[str, str]) -> list[str]:
        return self._models

    async def is_credential_available(self, credential: dict[str, str], model: str | None = None) -> bool:
        self.available_checks.append((credential, model))
        return self._available

    async def mark_success(self, credential: dict[str, str], meta: dict | None = None) -> None:
        self.success_calls.append((credential, meta or {}))

    async def mark_error(self, credential: dict[str, str], error_meta: dict | None = None) -> None:
        self.error_calls.append((credential, error_meta or {}))

    async def explain_credential(self, credential: dict[str, str]) -> dict:
        return {
            "provider": self._name,
            "available": self._available,
            "credential_hint": self.credential_hint,
        }

    async def get_capacity_signal(self, credential: dict[str, str]) -> CapacitySignal | None:
        key = tuple(sorted(credential.items()))
        if key in self._capacity_by_credential:
            return self._capacity_by_credential[key]
        return self._capacity_signal


def build_provider_registry(*plugins: ProviderPlugin) -> ProviderRegistry:
    registry = ProviderRegistry()
    for plugin in plugins:
        registry.register(plugin)
    return registry
