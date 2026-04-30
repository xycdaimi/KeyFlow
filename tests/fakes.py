"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-27
@Description: 测试用内存仓储与 Provider 假实现
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from domain.entities.api_key import ApiKey
from domain.exceptions.domain_exceptions import DuplicateCredentialError, UpstreamUnreachableError
from domain.value_objects.key_status import KeyStatus
from infrastructure.plugins.base import CapacitySignal, ProviderPlugin, ProviderRegistry


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryKeyRepository:
    def __init__(self, keys: list[ApiKey] | None = None) -> None:
        self._keys = {key.id: key for key in keys or []}
        self._runtime_locks: dict[str, tuple[str, datetime, str]] = {}

    def _assert_unique_provider_credential(self, key_id: str, provider: str, credential: dict[str, str]) -> None:
        candidate = json.dumps(credential, sort_keys=True)
        for existing in self._keys.values():
            if existing.id == key_id or existing.provider != provider:
                continue
            if json.dumps(existing.credential, sort_keys=True) == candidate:
                raise DuplicateCredentialError(
                    f"credential already exists for provider {provider} (key_id={existing.id})"
                )

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
        self._assert_unique_provider_credential(key.id, key.provider, key.credential)
        key.updated_at = utcnow()
        self._keys[key.id] = key
        return key

    async def touch_key_used(self, key_id: str, now: datetime) -> ApiKey | None:
        key = self._keys.get(key_id)
        if key is None:
            return None
        key.last_used_at = now
        key.updated_at = now
        return key

    async def record_success(self, key_id: str, tokens_used: int, now: datetime) -> ApiKey | None:
        key = self._keys.get(key_id)
        if key is None:
            return None
        key.success_count += 1
        key.quota_used += max(tokens_used, 0)
        key.last_used_at = now
        key.updated_at = now
        return key

    async def record_error(self, key_id: str, now: datetime) -> ApiKey | None:
        key = self._keys.get(key_id)
        if key is None:
            return None
        key.error_count += 1
        key.last_used_at = now
        key.updated_at = now
        return key

    async def update_status(
        self,
        key_id: str,
        status: str,
        cooldown_until: datetime | None,
        now: datetime,
    ) -> ApiKey | None:
        key = self._keys.get(key_id)
        if key is None:
            return None
        key.status = KeyStatus(status)
        key.cooldown_until = cooldown_until
        key.updated_at = now
        return key

    async def acquire_runtime_lock(
        self,
        key_id: str,
        owner: str,
        now: datetime,
        ttl_seconds: int,
        reason: str,
    ) -> bool:
        if key_id not in self._keys:
            return False
        current = self._runtime_locks.get(key_id)
        if current is not None:
            current_owner, lock_until, _ = current
            if lock_until > now and current_owner != owner:
                return False
        self._runtime_locks[key_id] = (
            owner,
            now + timedelta(seconds=max(ttl_seconds, 1)),
            reason,
        )
        return True

    async def release_runtime_lock(self, key_id: str, owner: str, now: datetime) -> None:
        current = self._runtime_locks.get(key_id)
        if current is None:
            return
        current_owner, _, _ = current
        if current_owner == owner:
            self._runtime_locks.pop(key_id, None)

    async def update_runtime_snapshot_if_locked(
        self,
        key: ApiKey,
        owner: str,
        now: datetime,
    ) -> ApiKey | None:
        current_lock = self._runtime_locks.get(key.id)
        if current_lock is None:
            return None
        current_owner, lock_until, _ = current_lock
        if current_owner != owner or lock_until <= now:
            return None
        current = self._keys.get(key.id)
        if current is None:
            return None
        self._assert_unique_provider_credential(key.id, key.provider, key.credential)
        current.credential = dict(key.credential)
        current.status = key.status
        current.cooldown_until = key.cooldown_until
        current.supported_models = list(key.supported_models)
        current.last_refreshed_at = key.last_refreshed_at
        current.cached_available = key.cached_available
        current.cached_quota_available = key.cached_quota_available
        current.cached_capacity_score = key.cached_capacity_score
        current.updated_at = now
        return current

    async def update_background_runtime_snapshot_if_locked(
        self,
        key: ApiKey,
        owner: str,
        now: datetime,
    ) -> ApiKey | None:
        current_lock = self._runtime_locks.get(key.id)
        if current_lock is None:
            return None
        current_owner, lock_until, _ = current_lock
        if current_owner != owner or lock_until <= now:
            return None
        current = self._keys.get(key.id)
        if current is None or current.status in {
            KeyStatus.DISABLED_ADMIN,
            KeyStatus.DISABLED_REPORT,
        }:
            return None
        self._assert_unique_provider_credential(key.id, key.provider, key.credential)
        current.credential = dict(key.credential)
        current.status = key.status
        current.cooldown_until = key.cooldown_until
        current.supported_models = list(key.supported_models)
        current.last_refreshed_at = key.last_refreshed_at
        current.cached_available = key.cached_available
        current.cached_quota_available = key.cached_quota_available
        current.cached_capacity_score = key.cached_capacity_score
        current.updated_at = now
        return current

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
        plugin_ready: bool = True,
        capacity_signal: CapacitySignal | None = None,
        capacity_by_credential: dict[tuple[tuple[str, str], ...], CapacitySignal | None] | None = None,
        upstream_root_reachable: bool = True,
    ) -> None:
        self._name = name
        self._models = models or []
        self._available = available
        self._plugin_ready = plugin_ready
        self._upstream_root_reachable = upstream_root_reachable
        self._capacity_signal = capacity_signal
        self._capacity_by_credential = capacity_by_credential or {}
        self.success_calls: list[tuple[dict[str, str], dict]] = []
        self.error_calls: list[tuple[dict[str, str], dict]] = []
        self.available_checks: list[dict[str, str]] = []

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

    def is_plugin_ready(self) -> bool:
        return self._plugin_ready

    async def verify_upstream_root_reachable(self) -> None:
        if not self._upstream_root_reachable:
            raise UpstreamUnreachableError(f"https://fake-{self._name}.test/")

    async def fetch_models(self, credential: dict[str, str]) -> list[str]:
        return self._models

    async def is_credential_available(self, credential: dict[str, str]) -> bool:
        self.available_checks.append(credential)
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


class FakeOauthProviderPlugin(FakeProviderPlugin):
    def __init__(
        self,
        name: str,
        models: list[str] | None = None,
        *,
        fresh: bool = True,
        refreshed_credential: dict[str, str] | None = None,
        refresh_result: dict[str, str] | None = None,
        available: bool = True,
        capacity_signal: CapacitySignal | None = None,
    ) -> None:
        super().__init__(name, models, available=available, capacity_signal=capacity_signal)
        self._fresh = fresh
        self._refreshed_credential = refreshed_credential
        self._refresh_result = refresh_result
        self.fresh_checks: list[dict[str, str]] = []
        self.refresh_calls: list[dict[str, str]] = []

    @property
    def auth_type(self) -> str:
        return "oauth_json"

    def _is_oauth_credential_fresh(self, credential: dict[str, str]) -> bool:
        self.fresh_checks.append(dict(credential))
        return self._fresh

    async def _refresh_oauth_credential(self, credential: dict[str, str]) -> dict[str, str] | None:
        self.refresh_calls.append(dict(credential))
        if self._refresh_result is None:
            return dict(self._refreshed_credential) if self._refreshed_credential is not None else None
        return dict(self._refresh_result)


def build_provider_registry(*plugins: ProviderPlugin) -> ProviderRegistry:
    registry = ProviderRegistry()
    for plugin in plugins:
        registry.register(plugin)
    return registry
