"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-03
@Description: Key 生命周期与分配应用服务
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from domain.entities.api_key import ApiKey
from domain.exceptions.domain_exceptions import (
    DuplicateCredentialError,
    KeyNotFoundError,
    NoAvailableKeyError,
    ProviderNotFoundError,
    ProviderNotReadyError,
)
from domain.repositories.key_repository import KeyAllocationStore, KeyRepository
from domain.services.scheduler import KeyScheduler
from domain.services.scorer import KeyScorer
from domain.services.state_machine import KeyStateMachine
from domain.value_objects.key_status import KeyStatus
from application.services.model_alias_resolver import ModelAliasResolver
from infrastructure.plugins.base import ProviderRegistry

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class CreateKeyInput:
    """Minimum input to add a new credential account."""

    provider: str
    credential: dict[str, str]


@dataclass(slots=True)
class UpdateKeyInput:
    """Fields that can be changed by the caller."""

    credential: dict[str, str] | None = None
    status: KeyStatus | None = None


@dataclass(slots=True)
class AllocationResult:
    key: ApiKey
    provider_model: str | None = None


class KeyService:
    _ADMIN_WRITABLE_STATUSES = frozenset({KeyStatus.AVAILABLE, KeyStatus.DISABLED_ADMIN})

    def __init__(
        self,
        repository: KeyRepository,
        allocation_store: KeyAllocationStore,
        scheduler: KeyScheduler,
        scorer: KeyScorer,
        state_machine: KeyStateMachine,
        provider_registry: ProviderRegistry,
        model_alias_resolver: ModelAliasResolver | None = None,
        allocation_lease_seconds: int = 2,
        refresh_cache_seconds: int = 60,
    ) -> None:
        self._repository = repository
        self._allocation_store = allocation_store
        self._scheduler = scheduler
        self._scorer = scorer
        self._state_machine = state_machine
        self._provider_registry = provider_registry
        self._model_alias_resolver = model_alias_resolver or ModelAliasResolver.empty()
        self._allocation_lease_seconds = max(allocation_lease_seconds, 1)
        self._refresh_cache_seconds = max(refresh_cache_seconds, 1)

    async def create_key(self, data: CreateKeyInput) -> ApiKey:
        now = utcnow()
        provider = data.provider.strip().lower()
        plugin = self._require_ready_provider(provider)
        existing = await self._repository.get_by_provider_credential(provider, data.credential)
        if existing is not None:
            raise DuplicateCredentialError(
                f"credential already exists for provider {provider} (key_id={existing.id})"
            )
        await plugin.verify_upstream_root_reachable()
        key = ApiKey(
            id=str(uuid4()),
            provider=provider,
            credential=data.credential,
        )
        await self._refresh_single_key(key, now, plugin=plugin)
        await self._sync_models(key, plugin=plugin)
        await self._repository.upsert_key(key)
        await self._allocation_store.sync_key(key, self._scorer.score(key, now))
        return key

    async def update_key(self, key_id: str, data: UpdateKeyInput) -> ApiKey:
        key = await self._get_required_key(key_id)
        if data.credential is not None:
            plugin = self._require_ready_provider(key.provider)
            existing = await self._repository.get_by_provider_credential(
                key.provider, data.credential
            )
            if existing is not None and existing.id != key.id:
                raise DuplicateCredentialError(
                    f"credential already exists for provider {key.provider} (key_id={existing.id})"
                )
            await plugin.verify_upstream_root_reachable()
            key.credential = data.credential
            await self._refresh_single_key(key, utcnow(), plugin=plugin)
            await self._sync_models(key, plugin=plugin)
        if data.status is not None:
            if data.status not in self._ADMIN_WRITABLE_STATUSES:
                raise ValueError("status must be one of: available, disabled_admin")
            key.status = data.status
        await self._repository.upsert_key(key)
        await self._allocation_store.sync_key(key, self._scorer.score(key, utcnow()))
        return key

    async def delete_key(self, key_id: str) -> None:
        key = await self._get_required_key(key_id)
        await self._repository.delete_key(key_id)
        await self._allocation_store.remove_key(key_id, key.provider)

    async def list_keys(self, provider: str | None = None) -> list[ApiKey]:
        return await self._repository.list_keys(provider=provider)

    async def get_key_explain(self, key_id: str) -> dict:
        """Return plugin explain_credential for admin display."""
        key = await self._get_required_key(key_id)
        plugin = self._provider_registry.get(key.provider)
        if plugin is None:
            return {"provider": key.provider, "status": "no_plugin"}
        try:
            return await plugin.explain_credential(key.credential)
        except Exception as exc:
            logger.warning("explain_credential failed for %s: %s", key.id, exc)
            return {"provider": key.provider, "status": "plugin_error", "error_code": "explain_failed"}

    async def get_key(self, key_id: str) -> ApiKey:
        return await self._get_required_key(key_id)

    async def get_key_models(self, provider: str, key_id: str) -> list[str]:
        key = await self._get_required_key(key_id)
        if key.provider != provider.strip().lower():
            raise KeyNotFoundError(f"key {key_id} not found for provider {provider}")
        return list(key.supported_models)

    async def allocate_key(self, provider: str, model: str | None = None) -> AllocationResult:
        now = utcnow()
        provider = provider.strip().lower()
        keys = await self._repository.list_provider_keys(provider)
        await self._recover_ready_keys(keys, now)
        candidates, capacity_by_key_id, provider_model_by_key_id = await self._collect_candidates(
            keys, model, now
        )

        ranked = self._scheduler.rank_keys(candidates, now, capacity_by_key_id=capacity_by_key_id)
        if not ranked:
            raise NoAvailableKeyError("no available key")

        ordered_ids = [item.key.id for item in ranked]
        allocated_id = await self._allocation_store.allocate_key(
            provider,
            ordered_ids,
            now,
            lease_seconds=self._allocation_lease_seconds,
        )
        if allocated_id is None:
            raise NoAvailableKeyError("no available key")

        return await self._finalize_allocation(ranked, allocated_id, now, provider_model_by_key_id)

    async def allocate_key_by_model(self, model: str) -> AllocationResult:
        now = utcnow()
        keys = await self._repository.list_keys()
        await self._recover_ready_keys(keys, now)
        candidates, capacity_by_key_id, provider_model_by_key_id = await self._collect_candidates(
            keys, model, now
        )

        ranked = self._scheduler.rank_keys(candidates, now, capacity_by_key_id=capacity_by_key_id)
        if not ranked:
            raise NoAvailableKeyError("no available key")

        allocated_id = await self._allocation_store.allocate_key_any_provider(
            [item.key for item in ranked],
            now,
            lease_seconds=self._allocation_lease_seconds,
        )
        if allocated_id is None:
            raise NoAvailableKeyError("no available key")

        return await self._finalize_allocation(ranked, allocated_id, now, provider_model_by_key_id)

    async def _recover_ready_keys(self, keys: list[ApiKey], now: datetime) -> None:
        for key in keys:
            before = key.status
            self._state_machine.recover_if_ready(key, now)
            if before != key.status:
                await self._repository.upsert_key(key)

    def _is_cache_fresh(self, key: ApiKey, now: datetime) -> bool:
        """True if key has fresh cached availability/capacity (no plugin calls needed)."""
        if key.last_refreshed_at is None:
            return False
        return (now - key.last_refreshed_at).total_seconds() < self._refresh_cache_seconds

    async def _collect_candidates(
        self,
        keys: list[ApiKey],
        model: str | None,
        now: datetime,
    ) -> tuple[list[ApiKey], dict[str, float | None], dict[str, str | None]]:
        """Use cached refresh freshness and key status. No plugin calls during allocation."""
        candidates: list[ApiKey] = []
        capacity_by_key_id: dict[str, float | None] = {}
        provider_model_by_key_id: dict[str, str | None] = {}

        for key in keys:
            if not key.is_available(now):
                continue

            provider_model: str | None = None
            if model:
                provider_model = self._model_alias_resolver.resolve_provider_model(
                    requested_model=model,
                    provider=key.provider,
                    supported_models=list(key.supported_models),
                )
                if provider_model is None:
                    continue

            plugin = self._provider_registry.get(key.provider)
            if plugin is not None:
                if not self._is_cache_fresh(key, now):
                    continue
                capacity_by_key_id[key.id] = key.cached_capacity_score
            else:
                capacity_by_key_id[key.id] = None

            provider_model_by_key_id[key.id] = provider_model
            candidates.append(key)

        return candidates, capacity_by_key_id, provider_model_by_key_id

    async def _finalize_allocation(
        self,
        ranked: list,
        allocated_id: str,
        now: datetime,
        provider_model_by_key_id: dict[str, str | None],
    ) -> AllocationResult:
        selected = next((item.key for item in ranked if item.key.id == allocated_id), None)
        if selected is None:
            selected = await self._get_required_key(allocated_id)

        selected.mark_used(now)
        await self._repository.upsert_key(selected)
        await self._allocation_store.sync_key(selected, self._scorer.score(selected, now))
        return AllocationResult(
            key=selected,
            provider_model=provider_model_by_key_id.get(selected.id),
        )

    async def report_success(self, key_id: str, tokens_used: int = 0) -> ApiKey:
        now = utcnow()
        key = await self._get_required_key(key_id)
        self._state_machine.on_success(key, tokens_used=tokens_used, now=now)
        await self._repository.upsert_key(key)
        await self._allocation_store.sync_key(key, self._scorer.score(key, now))
        await self._allocation_store.release_key_lease(key.provider, key.id)
        plugin = self._provider_registry.get(key.provider)
        if plugin is not None:
            try:
                await plugin.mark_success(key.credential, {"tokens_used": tokens_used})
            except Exception as exc:
                logger.warning("mark_success failed for %s: %s", key.id, exc)
        return key

    async def report_error(self, key_id: str, error_type: str) -> ApiKey:
        now = utcnow()
        key = await self._get_required_key(key_id)
        self._state_machine.on_error(key, error_type=error_type, now=now)
        await self._repository.upsert_key(key)
        await self._allocation_store.sync_key(key, self._scorer.score(key, now))
        await self._allocation_store.release_key_lease(key.provider, key.id)
        plugin = self._provider_registry.get(key.provider)
        if plugin is not None:
            try:
                await plugin.mark_error(key.credential, {"error_type": error_type})
            except Exception as exc:
                logger.warning("mark_error failed for %s: %s", key.id, exc)
        return key

    async def recover_cooldowns(self) -> int:
        now = utcnow()
        keys = await self._repository.list_recoverable_keys(now)
        recovered_count = 0
        for key in keys:
            before = key.status
            self._state_machine.recover_if_ready(key, now)
            if before != key.status:
                recovered_count += 1
            await self._repository.upsert_key(key)
            await self._allocation_store.sync_key(key, self._scorer.score(key, now))
        return recovered_count

    async def refresh_keys(self, model: str | None = None) -> int:
        """Refresh availability/capacity for keys needing it."""
        now = utcnow()
        cutoff = now - timedelta(seconds=self._refresh_cache_seconds)
        keys = await self._repository.list_keys_needing_refresh(cutoff)
        refreshed = 0
        for key in keys:
            if not await self._repository.claim_refresh(key.id, now, self._refresh_cache_seconds):
                continue
            plugin = self._provider_registry.get(key.provider)
            if plugin is None:
                key.cached_available = True
                key.cached_quota_available = None
                key.cached_capacity_score = None
                key.last_refreshed_at = now
                self._merge_refresh_signals_into_status(key, now)
                await self._repository.upsert_key(key)
                refreshed += 1
                continue

            key.cached_available = await self._probe_credential_available(key, plugin, model)
            key.cached_quota_available, key.cached_capacity_score = await self._probe_capacity(key, plugin)
            key.last_refreshed_at = now
            self._merge_refresh_signals_into_status(key, now)
            await self._sync_models(key, plugin=plugin)
            await self._repository.upsert_key(key)
            await self._allocation_store.sync_key(key, self._scorer.score(key, now))
            refreshed += 1
        return refreshed

    async def _refresh_single_key(self, key: ApiKey, now: datetime, plugin=None) -> None:
        """Refresh one key's availability/capacity cache and merge status."""
        plugin = plugin or self._provider_registry.get(key.provider)
        if plugin is None:
            key.cached_available = True
            key.cached_quota_available = None
            key.cached_capacity_score = None
            key.last_refreshed_at = now
            self._merge_refresh_signals_into_status(key, now)
            return

        key.cached_available = await self._probe_credential_available(key, plugin, None)
        key.cached_quota_available, key.cached_capacity_score = await self._probe_capacity(key, plugin)
        key.last_refreshed_at = now
        self._merge_refresh_signals_into_status(key, now)

    async def _probe_credential_available(self, key: ApiKey, plugin, model: str | None) -> bool:
        try:
            return await plugin.is_credential_available(key.credential, model)
        except Exception as exc:
            logger.warning("is_credential_available failed for %s: %s", key.id, exc)
            return False

    async def _probe_capacity(self, key: ApiKey, plugin) -> tuple[bool | None, float | None]:
        try:
            signal = await plugin.get_capacity_signal(key.credential)
        except Exception as exc:
            logger.warning("get_capacity_signal failed for %s: %s", key.id, exc)
            return None, None
        if signal is None:
            return None, None
        quota_available = getattr(signal, "quota_available", None)
        return quota_available, signal.capacity_score

    def _merge_refresh_signals_into_status(self, key: ApiKey, now: datetime) -> None:
        if key.status in {KeyStatus.DISABLED_ADMIN, KeyStatus.DISABLED_REPORT}:
            return

        if key.cached_available is False:
            key.status = KeyStatus.DISABLED_UPSTREAM
            return

        if key.cached_available is True and key.cached_quota_available is False:
            key.status = KeyStatus.EXHAUSTED
            return

        if key.status == KeyStatus.DISABLED_UPSTREAM and key.cached_available is True:
            key.status = KeyStatus.AVAILABLE

        if key.status == KeyStatus.EXHAUSTED and key.cached_quota_available is True:
            key.status = KeyStatus.AVAILABLE

        self._state_machine.recover_if_ready(key, now)

    async def _get_required_key(self, key_id: str) -> ApiKey:
        key = await self._repository.get_key(key_id)
        if key is None:
            raise KeyNotFoundError(f"key {key_id} not found")
        return key

    async def _sync_models(self, key: ApiKey, plugin=None) -> None:
        """Fetch and store provider model list. Failures do not change status."""
        plugin = plugin or self._provider_registry.get(key.provider)
        if plugin is None:
            return
        try:
            key.supported_models = await plugin.fetch_models(key.credential)
        except Exception as exc:
            key.supported_models = []
            logger.warning("fetch_models failed for %s: %s", key.id, exc)

    def _require_ready_provider(self, provider: str):
        plugin = self._provider_registry.get(provider)
        if plugin is None:
            raise ProviderNotFoundError(f"provider {provider} is not registered")
        if not plugin.is_plugin_ready():
            raise ProviderNotReadyError(f"provider {provider} is not ready")
        return plugin
