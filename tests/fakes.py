from __future__ import annotations

from datetime import datetime

from domain.entities.api_key import ApiKey
from infrastructure.plugins.base import ProviderPlugin, ProviderRegistry


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


class InMemoryAllocationStore:
    def __init__(self) -> None:
        self.synced_scores: dict[str, float] = {}

    async def sync_key(self, key: ApiKey, score: float) -> None:
        self.synced_scores[key.id] = score

    async def remove_key(self, key_id: str, provider: str) -> None:
        self.synced_scores.pop(key_id, None)

    async def allocate_key(self, provider: str, ordered_key_ids: list[str], now: datetime) -> str | None:
        if not ordered_key_ids:
            return None
        return ordered_key_ids[0]


class FakeProviderPlugin(ProviderPlugin):
    """In-memory plugin for unit tests.

    ``available`` controls the return value of is_credential_available.
    """

    def __init__(
        self,
        name: str,
        models: list[str] | None = None,
        available: bool = True,
    ) -> None:
        self._name = name
        self._models = models or []
        self._available = available
        self.success_calls: list[tuple[str, dict]] = []
        self.error_calls: list[tuple[str, dict]] = []

    @property
    def name(self) -> str:
        return self._name

    async def fetch_models(self, api_key: str) -> list[str]:
        return self._models

    async def is_credential_available(self, api_key: str, model: str | None = None) -> bool:
        return self._available

    async def mark_success(self, api_key: str, meta: dict | None = None) -> None:
        self.success_calls.append((api_key, meta or {}))

    async def mark_error(self, api_key: str, error_meta: dict | None = None) -> None:
        self.error_calls.append((api_key, error_meta or {}))

    async def explain_credential(self, api_key: str) -> dict:
        return {"provider": self._name, "available": self._available}


def build_provider_registry(*plugins: ProviderPlugin) -> ProviderRegistry:
    registry = ProviderRegistry()
    for plugin in plugins:
        registry.register(plugin)
    return registry
