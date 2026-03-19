from __future__ import annotations

from datetime import datetime

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
        self.released: list[tuple[str, str]] = []

    async def sync_key(self, key: ApiKey, score: float) -> None:
        self.synced_scores[key.id] = score

    async def remove_key(self, key_id: str, provider: str) -> None:
        self.synced_scores.pop(key_id, None)

    async def allocate_key(
        self,
        provider: str,
        ordered_key_ids: list[str],
        now: datetime,
        lease_seconds: int = 2,
    ) -> str | None:
        if not ordered_key_ids:
            return None
        return ordered_key_ids[0]

    async def release_key_lease(self, provider: str, key_id: str) -> None:
        self.released.append((provider, key_id))


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
