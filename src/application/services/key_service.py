"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-29
@Description: Key 生命周期与分配应用服务
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from domain.entities.api_key import ApiKey
from domain.exceptions.domain_exceptions import (
    AllocationStoreUnavailableError,
    DuplicateCredentialError,
    InvalidCredentialError,
    KeyNotFoundError,
    NoAvailableKeyError,
    ProviderNotFoundError,
    ProviderNotReadyError,
    RuntimeLockUnavailableError,
)
from domain.repositories.key_repository import KeyAllocationStore, KeyRepository
from domain.services.scheduler import KeyScheduler
from domain.services.scorer import KeyScorer
from domain.services.state_machine import KeyStateMachine
from domain.value_objects.key_pool import KeyPool
from domain.value_objects.key_status import KeyStatus
from application.services.model_alias_resolver import ModelAliasResolver
from infrastructure.plugins.base import ProviderRegistry

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _allocation_pool_sequence(pool: KeyPool) -> tuple[KeyPool, ...]:
    if pool == KeyPool.VIP:
        return (KeyPool.VIP, KeyPool.DEFAULT)
    return (KeyPool.DEFAULT,)


@dataclass(slots=True)
class CreateKeyInput:
    """Minimum input to add a new credential account."""

    provider: str
    credential: dict[str, Any]
    pool: KeyPool = KeyPool.DEFAULT
    max_concurrent_uses: int = 1


@dataclass(slots=True)
class UpdateKeyInput:
    """Fields that can be changed by the caller."""

    credential: dict[str, Any] | None = None
    status: KeyStatus | None = None
    max_concurrent_uses: int | None = None


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
        pending_validation_grace_seconds: int = 60,
        runtime_lock_seconds: int = 300,
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
        self._pending_validation_grace_seconds = max(pending_validation_grace_seconds, 1)
        self._runtime_lock_seconds = max(runtime_lock_seconds, 1)

    async def create_key(self, data: CreateKeyInput) -> ApiKey:
        now = utcnow()
        provider = data.provider.strip().lower()
        plugin = self._require_ready_provider(provider)
        credential = await self._prepare_registration_credential(data.credential, plugin)
        existing = await self._repository.get_by_provider_credential(provider, credential)
        if existing is not None:
            raise DuplicateCredentialError(
                f"credential already exists for provider {provider} (key_id={existing.id})"
            )
        await plugin.verify_upstream_root_reachable()
        key = ApiKey(
            id=str(uuid4()),
            provider=provider,
            credential=credential,
            pool=data.pool,
            max_concurrent_uses=max(data.max_concurrent_uses, 1),
            status=KeyStatus.PENDING,
            last_refreshed_at=now,
        )
        await self._repository.upsert_key(key)
        await self._allocation_store.sync_key(key, self._scorer.score(key, now))
        if key.status == KeyStatus.PENDING:
            validated = await self.validate_pending_key(key.id)
            latest = await self._repository.get_key(key.id) if validated else None
            if latest is not None:
                return latest
            self._schedule_pending_validation(key.id)
        return key

    async def update_key(self, key_id: str, data: UpdateKeyInput) -> ApiKey:
        key = await self._get_required_key(key_id)
        original_status = key.status
        restore_admin_with_credential = (
            data.credential is not None
            and data.status == KeyStatus.AVAILABLE
            and original_status == KeyStatus.DISABLED_ADMIN
        )
        credential_handled_admin_restore = False
        if data.credential is not None:
            plugin = self._require_ready_provider(key.provider)
            credential = await self._prepare_registration_credential(data.credential, plugin)
            existing = await self._repository.get_by_provider_credential(
                key.provider, credential
            )
            if existing is not None and existing.id != key.id:
                raise DuplicateCredentialError(
                    f"credential already exists for provider {key.provider} (key_id={existing.id})"
                )
            owner = f"update_key:{uuid4()}"
            now = utcnow()
            if not await self._repository.acquire_runtime_lock(
                key.id, owner, now, self._runtime_lock_seconds, "update_key"
            ):
                raise RuntimeLockUnavailableError(f"key {key_id} runtime snapshot is locked")
            try:
                await plugin.verify_upstream_root_reachable()
                key.credential = credential
                if restore_admin_with_credential:
                    key.status = KeyStatus.AVAILABLE
                    key.cooldown_until = None
                sync_credential = await self._refresh_single_key(key, utcnow(), plugin=plugin)
                if sync_credential is not None:
                    await self._sync_models(key, plugin=plugin, credential=sync_credential)
                latest = await self._repository.get_key(key.id)
                if latest is not None:
                    if restore_admin_with_credential:
                        latest.credential = key.credential
                        latest.status = key.status
                        latest.cooldown_until = key.cooldown_until
                        latest.supported_models = list(key.supported_models)
                        latest.last_refreshed_at = key.last_refreshed_at
                        latest.cached_available = key.cached_available
                        latest.cached_quota_available = key.cached_quota_available
                        latest.cached_capacity_score = key.cached_capacity_score
                    else:
                        self._merge_runtime_mutation(latest, key)
                    key = latest
                persisted = await self._repository.update_runtime_snapshot_if_locked(
                    key, owner, utcnow()
                )
                if persisted is None:
                    raise RuntimeLockUnavailableError(f"key {key_id} runtime snapshot is locked")
                key = persisted
                credential_handled_admin_restore = restore_admin_with_credential
            finally:
                await self._repository.release_runtime_lock(key.id, owner, utcnow())
        if data.status is not None and not credential_handled_admin_restore:
            if data.status not in self._ADMIN_WRITABLE_STATUSES:
                raise ValueError("status must be one of: available, disabled_admin")
            if data.status == KeyStatus.DISABLED_ADMIN:
                persisted = await self._repository.update_status(
                    key.id,
                    data.status.value,
                    None,
                    utcnow(),
                )
                if persisted is None:
                    raise KeyNotFoundError(f"key {key_id} not found")
                key = persisted
            elif key.status != KeyStatus.DISABLED_ADMIN:
                persisted = await self._repository.update_status(
                    key.id,
                    data.status.value,
                    None,
                    utcnow(),
                )
                if persisted is None:
                    raise KeyNotFoundError(f"key {key_id} not found")
                key = persisted
            else:
                plugin = self._require_ready_provider(key.provider)
                now = utcnow()
                owner = f"restore_admin:{uuid4()}"
                if not await self._repository.acquire_runtime_lock(
                    key.id, owner, now, self._runtime_lock_seconds, "restore_admin"
                ):
                    raise RuntimeLockUnavailableError(f"key {key_id} runtime snapshot is locked")
                try:
                    key.cooldown_until = None
                    probe_credential = await self._run_key_preflight(key, plugin)
                    if probe_credential is None:
                        key.supported_models = []
                        key.cached_available = False
                        key.cached_quota_available = None
                        key.cached_capacity_score = None
                        key.last_refreshed_at = now
                        key.status = KeyStatus.DISABLED_UPSTREAM
                    else:
                        try:
                            key.cached_available = await plugin.is_credential_available(
                                probe_credential
                            )
                        except Exception as exc:
                            logger.warning(
                                "is_credential_available failed for %s: %s", key.id, exc
                            )
                            key.cached_available = False

                        if key.cached_available is False:
                            key.supported_models = []
                            key.cached_quota_available = None
                            key.cached_capacity_score = None
                            key.last_refreshed_at = now
                            key.status = KeyStatus.DISABLED_UPSTREAM
                        else:
                            try:
                                signal = await plugin.get_capacity_signal(probe_credential)
                            except Exception as exc:
                                logger.warning(
                                    "get_capacity_signal failed for %s: %s", key.id, exc
                                )
                                signal = None

                            key.cached_quota_available = (
                                None if signal is None else signal.quota_available
                            )
                            key.cached_capacity_score = (
                                None if signal is None else signal.capacity_score
                            )
                            key.last_refreshed_at = now
                            await self._sync_models(
                                key, plugin=plugin, credential=probe_credential
                            )
                            if key.cached_quota_available is False:
                                key.status = KeyStatus.EXHAUSTED
                            else:
                                key.status = KeyStatus.AVAILABLE

                    persisted = await self._repository.update_runtime_snapshot_if_locked(
                        key,
                        owner,
                        now,
                    )
                    if persisted is None:
                        raise RuntimeLockUnavailableError(f"key {key_id} runtime snapshot is locked")
                    key = persisted
                finally:
                    await self._repository.release_runtime_lock(key.id, owner, utcnow())
        if data.max_concurrent_uses is not None:
            persisted = await self._repository.update_max_concurrent_uses(
                key.id,
                data.max_concurrent_uses,
            )
            if persisted is None:
                raise KeyNotFoundError(f"key {key_id} not found")
            key = persisted
        await self._allocation_store.sync_key(key, self._scorer.score(key, utcnow()))
        return key

    async def delete_key(self, key_id: str) -> None:
        key = await self._get_required_key(key_id)
        await self._repository.delete_key(key_id)
        await self._allocation_store.remove_key(key_id, key.provider, key.pool)

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

    async def move_key_pool(self, key_id: str, pool: KeyPool) -> ApiKey:
        key = await self._get_required_key(key_id)
        if key.pool == pool:
            return key

        migrated = await self._repository.update_pool(key.id, pool)
        if migrated is None:
            raise KeyNotFoundError(f"key {key_id} not found")
        await self._allocation_store.remove_key(key.id, key.provider, key.pool)
        await self._allocation_store.sync_key(migrated, self._scorer.score(migrated, utcnow()))
        return migrated

    async def allocate_key(
        self,
        provider: str,
        model: str | None = None,
        pool: KeyPool = KeyPool.DEFAULT,
    ) -> AllocationResult:
        now = utcnow()
        provider = provider.strip().lower()
        last_error: NoAvailableKeyError | None = None
        for candidate_pool in _allocation_pool_sequence(pool):
            keys = await self._repository.list_provider_pool_keys(provider, candidate_pool)
            await self._recover_ready_keys(keys, now)
            candidates, capacity_by_key_id, provider_model_by_key_id = await self._collect_candidates(
                keys, model, now
            )

            ranked = self._scheduler.rank_keys(candidates, now, capacity_by_key_id=capacity_by_key_id)
            if not ranked:
                last_error = NoAvailableKeyError("no available key")
                continue

            remaining_ranked = list(ranked)
            cache_repaired = False
            while remaining_ranked:
                ordered_ids = [item.key.id for item in remaining_ranked]
                allocated_id = await self._allocate_provider_candidate(
                    provider,
                    candidate_pool,
                    ordered_ids,
                    now,
                )
                if allocated_id is None:
                    if not cache_repaired:
                        await self._sync_ranked_candidates(remaining_ranked, now)
                        cache_repaired = True
                        continue
                    last_error = NoAvailableKeyError("no available key")
                    break

                try:
                    return await self._finalize_allocation(
                        remaining_ranked,
                        allocated_id,
                        now,
                        provider_model_by_key_id,
                        lease_provider=provider,
                        lease_pool=candidate_pool,
                        requested_model=model,
                    )
                except NoAvailableKeyError as exc:
                    last_error = exc
                    remaining_ranked = [
                        item for item in remaining_ranked if item.key.id != allocated_id
                    ]
                    continue

        raise last_error or NoAvailableKeyError("no available key")

    async def allocate_key_by_model(
        self,
        model: str,
        pool: KeyPool = KeyPool.DEFAULT,
    ) -> AllocationResult:
        now = utcnow()
        last_error: NoAvailableKeyError | None = None
        for candidate_pool in _allocation_pool_sequence(pool):
            keys = await self._repository.list_pool_keys(candidate_pool)
            await self._recover_ready_keys(keys, now)
            candidates, capacity_by_key_id, provider_model_by_key_id = await self._collect_candidates(
                keys, model, now
            )

            ranked = self._scheduler.rank_keys(candidates, now, capacity_by_key_id=capacity_by_key_id)
            if not ranked:
                last_error = NoAvailableKeyError("no available key")
                continue

            remaining_ranked = list(ranked)
            cache_repaired = False
            while remaining_ranked:
                allocated_id = await self._allocate_any_provider_candidate(
                    candidate_pool,
                    [item.key for item in remaining_ranked],
                    now,
                )
                if allocated_id is None:
                    if not cache_repaired:
                        await self._sync_ranked_candidates(remaining_ranked, now)
                        cache_repaired = True
                        continue
                    last_error = NoAvailableKeyError("no available key")
                    break

                lease_provider = self._ranked_provider_for_key(remaining_ranked, allocated_id)
                if lease_provider is None:
                    last_error = NoAvailableKeyError("no available key")
                    remaining_ranked = [
                        item for item in remaining_ranked if item.key.id != allocated_id
                    ]
                    continue
                try:
                    return await self._finalize_allocation(
                        remaining_ranked,
                        allocated_id,
                        now,
                        provider_model_by_key_id,
                        lease_provider=lease_provider,
                        lease_pool=candidate_pool,
                        requested_model=model,
                    )
                except NoAvailableKeyError as exc:
                    last_error = exc
                    remaining_ranked = [
                        item for item in remaining_ranked if item.key.id != allocated_id
                    ]
                    continue

        raise last_error or NoAvailableKeyError("no available key")

    async def _allocate_provider_candidate(
        self,
        provider: str,
        pool: KeyPool,
        ordered_ids: list[str],
        now: datetime,
    ) -> str | None:
        try:
            return await self._allocation_store.allocate_key(
                provider,
                pool,
                ordered_ids,
                now,
                lease_seconds=self._allocation_lease_seconds,
            )
        except AllocationStoreUnavailableError:
            raise
        except Exception as exc:
            logger.warning("allocation store provider allocation failed: %s", exc)
            raise AllocationStoreUnavailableError("allocation store provider allocation failed") from exc

    async def _allocate_any_provider_candidate(
        self,
        pool: KeyPool,
        ordered_keys: list[ApiKey],
        now: datetime,
    ) -> str | None:
        try:
            return await self._allocation_store.allocate_key_any_provider(
                pool,
                ordered_keys,
                now,
                lease_seconds=self._allocation_lease_seconds,
            )
        except AllocationStoreUnavailableError:
            raise
        except Exception as exc:
            logger.warning("allocation store model allocation failed: %s", exc)
            raise AllocationStoreUnavailableError("allocation store model allocation failed") from exc

    async def _recover_ready_keys(self, keys: list[ApiKey], now: datetime) -> None:
        for key in keys:
            before = key.status
            self._state_machine.recover_if_ready(key, now)
            if before != key.status:
                persisted = await self._repository.update_status(
                    key.id,
                    key.status.value,
                    key.cooldown_until,
                    now,
                )
                if persisted is not None:
                    await self._allocation_store.sync_key(persisted, self._scorer.score(persisted, now))

    async def _collect_candidates(
        self,
        keys: list[ApiKey],
        model: str | None,
        now: datetime,
    ) -> tuple[list[ApiKey], dict[str, float | None], dict[str, str | None]]:
        """Build allocatable candidates without provider probing during allocation."""
        candidates: list[ApiKey] = []
        capacity_by_key_id: dict[str, float | None] = {}
        provider_model_by_key_id: dict[str, str | None] = {}

        for key in keys:
            if not key.is_available(now):
                continue
            if self._provider_registry.get(key.provider) is None:
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

            capacity_by_key_id[key.id] = key.cached_capacity_score
            provider_model_by_key_id[key.id] = provider_model
            candidates.append(key)

        return candidates, capacity_by_key_id, provider_model_by_key_id

    async def _sync_ranked_candidates(self, ranked: list, now: datetime) -> None:
        for item in ranked:
            await self._allocation_store.sync_key(item.key, self._scorer.score(item.key, now))

    @staticmethod
    def _ranked_provider_for_key(ranked: list, key_id: str) -> str | None:
        for item in ranked:
            if item.key.id == key_id:
                return item.key.provider
        return None

    @staticmethod
    def _contains_ranked_key(ranked: list, key_id: str) -> bool:
        return any(item.key.id == key_id for item in ranked)

    async def _finalize_allocation(
        self,
        ranked: list,
        allocated_id: str,
        now: datetime,
        provider_model_by_key_id: dict[str, str | None],
        lease_provider: str,
        lease_pool: KeyPool,
        requested_model: str | None,
    ) -> AllocationResult:
        try:
            selected = await self._get_required_key(allocated_id)
        except KeyNotFoundError as exc:
            await self._allocation_store.release_key_lease(lease_provider, lease_pool, allocated_id)
            raise NoAvailableKeyError("no available key") from exc

        provider_model = self._resolve_current_provider_model(selected, requested_model)
        if (
            not self._contains_ranked_key(ranked, allocated_id)
            or selected.id != allocated_id
            or selected.provider != lease_provider
            or selected.pool != lease_pool
            or not selected.is_available(now)
            or self._provider_registry.get(selected.provider) is None
            or (requested_model is not None and provider_model is None)
        ):
            await self._allocation_store.release_key_lease(lease_provider, lease_pool, allocated_id)
            raise NoAvailableKeyError("no available key")
        persisted = await self._repository.touch_key_used(selected.id, now)
        if persisted is None:
            await self._allocation_store.release_key_lease(lease_provider, lease_pool, allocated_id)
            raise NoAvailableKeyError("no available key")
        selected = persisted
        provider_model = self._resolve_current_provider_model(selected, requested_model)
        if (
            selected.id != allocated_id
            or selected.provider != lease_provider
            or selected.pool != lease_pool
            or not selected.is_available(now)
            or self._provider_registry.get(selected.provider) is None
            or (requested_model is not None and provider_model is None)
        ):
            await self._allocation_store.release_key_lease(lease_provider, lease_pool, allocated_id)
            raise NoAvailableKeyError("no available key")
        await self._allocation_store.sync_key(selected, self._scorer.score(selected, now))
        result_provider_model = (
            provider_model
            if requested_model is not None
            else provider_model_by_key_id.get(selected.id)
        )
        return AllocationResult(
            key=selected,
            provider_model=result_provider_model,
        )

    def _resolve_current_provider_model(self, key: ApiKey, requested_model: str | None) -> str | None:
        if requested_model is None:
            return None
        return self._model_alias_resolver.resolve_provider_model(
            requested_model=requested_model,
            provider=key.provider,
            supported_models=list(key.supported_models),
        )

    async def report_success(self, key_id: str, tokens_used: int = 0) -> ApiKey:
        now = utcnow()
        key = await self._get_required_key(key_id)
        persisted = await self._repository.record_success(key_id, tokens_used, now)
        if persisted is None:
            raise KeyNotFoundError(f"key {key_id} not found")
        key = persisted
        await self._allocation_store.sync_key(key, self._scorer.score(key, now))
        plugin = self._provider_registry.get(key.provider)
        if plugin is not None:
            try:
                await plugin.mark_success(key.credential, {"tokens_used": tokens_used})
            except Exception as exc:
                logger.warning("mark_success failed for %s: %s", key.id, exc)
        await self._allocation_store.release_key_lease(key.provider, key.pool, key.id)
        return key

    async def report_error(self, key_id: str, error_type: str) -> ApiKey:
        now = utcnow()
        key = await self._get_required_key(key_id)
        persisted = await self._repository.record_error(key_id, now)
        if persisted is None:
            raise KeyNotFoundError(f"key {key_id} not found")
        key = persisted
        await self._allocation_store.sync_key(key, self._scorer.score(key, now))
        plugin = self._provider_registry.get(key.provider)
        if plugin is not None:
            try:
                await plugin.mark_error(key.credential, {"error_type": error_type})
            except Exception as exc:
                logger.warning("mark_error failed for %s: %s", key.id, exc)
        await self._allocation_store.release_key_lease(key.provider, key.pool, key.id)
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
            persisted = await self._repository.update_status(
                key.id,
                key.status.value,
                key.cooldown_until,
                now,
            )
            if persisted is not None:
                key = persisted
            else:
                continue
            await self._allocation_store.sync_key(key, self._scorer.score(key, now))
        return recovered_count

    async def refresh_keys(self) -> int:
        """Refresh availability/capacity for keys needing it."""
        now = utcnow()
        cutoff = now - timedelta(seconds=self._refresh_cache_seconds)
        keys_by_id = {key.id: key for key in await self._repository.list_keys_needing_refresh(cutoff)}
        stale_pending = [
            key
            for key in await self._repository.list_keys()
            if key.status == KeyStatus.PENDING and not self._is_fresh_pending(key, now)
        ]
        for key in stale_pending:
            keys_by_id.setdefault(key.id, key)

        refreshed = 0
        for key in keys_by_id.values():
            if key.status == KeyStatus.PENDING and self._is_fresh_pending(key, now):
                continue

            owner = f"refresh_keys:{uuid4()}"
            if not await self._repository.acquire_runtime_lock(
                key.id, owner, now, self._runtime_lock_seconds, "refresh_keys"
            ):
                continue
            try:
                latest = await self._repository.get_key(key.id)
                if latest is None:
                    continue
                if latest.status in {KeyStatus.DISABLED_ADMIN, KeyStatus.DISABLED_REPORT}:
                    continue
                if latest.status == KeyStatus.PENDING:
                    if self._is_fresh_pending(latest, now):
                        continue
                elif latest.last_refreshed_at is not None and latest.last_refreshed_at >= cutoff:
                    continue
                key = latest
                plugin = self._provider_registry.get(key.provider)
                if plugin is None:
                    self._clear_supported_models_on_failure(key)
                    key.cached_available = False
                    key.cached_quota_available = None
                    key.cached_capacity_score = None
                    key.last_refreshed_at = now
                    self._merge_refresh_signals_into_status(key, now)
                    persisted = await self._persist_background_runtime_key(key, now, owner)
                    if persisted is None:
                        continue
                    await self._allocation_store.sync_key(persisted, self._scorer.score(persisted, now))
                    refreshed += 1
                    continue

                sync_credential = await self._refresh_single_key(
                    key,
                    now,
                    plugin=plugin,
                )
                if sync_credential is not None:
                    await self._sync_models(key, plugin=plugin, credential=sync_credential)
                persisted = await self._persist_background_runtime_key(key, now, owner)
                if persisted is None:
                    continue
                await self._allocation_store.sync_key(persisted, self._scorer.score(persisted, now))
                refreshed += 1
            finally:
                await self._repository.release_runtime_lock(key.id, owner, utcnow())
        return refreshed

    def _schedule_pending_validation(self, key_id: str) -> None:
        try:
            asyncio.create_task(self._run_pending_validation_task(key_id))
        except RuntimeError as exc:
            logger.warning("schedule pending validation failed for %s: %s", key_id, exc)

    async def _run_pending_validation_task(self, key_id: str) -> None:
        try:
            await self.validate_pending_key(key_id)
        except Exception as exc:
            logger.warning("pending validation failed for %s: %s", key_id, exc)

    async def validate_pending_key(self, key_id: str) -> bool:
        now = utcnow()
        key = await self._repository.get_key(key_id)
        if key is None or key.status != KeyStatus.PENDING:
            return False

        owner = f"pending_validation:{uuid4()}"
        if not await self._repository.acquire_runtime_lock(
            key.id, owner, now, self._runtime_lock_seconds, "pending_validation"
        ):
            return False
        try:
            latest = await self._repository.get_key(key.id)
            if latest is None or latest.status != KeyStatus.PENDING:
                return False
            key = latest
            plugin = self._provider_registry.get(key.provider)
            sync_credential = await self._refresh_single_key(key, now, plugin=plugin)
            if sync_credential is not None:
                await self._sync_models(key, plugin=plugin, credential=sync_credential)
            persisted = await self._persist_background_runtime_key(key, now, owner)
            if persisted is None:
                return False
            await self._allocation_store.sync_key(persisted, self._scorer.score(persisted, now))
            return True
        finally:
            await self._repository.release_runtime_lock(key.id, owner, utcnow())

    async def _refresh_single_key(
        self,
        key: ApiKey,
        now: datetime,
        plugin=None,
    ) -> dict[str, Any] | None:
        """Refresh one key's availability/capacity cache and merge status."""
        plugin = plugin or self._provider_registry.get(key.provider)
        if plugin is None:
            self._clear_supported_models_on_failure(key)
            key.cached_available = False
            key.cached_quota_available = None
            key.cached_capacity_score = None
            key.last_refreshed_at = now
            self._merge_refresh_signals_into_status(key, now)
            return None

        probe_credential = await self._run_key_preflight(key, plugin)
        if probe_credential is None:
            self._clear_supported_models_on_failure(key)
            key.cached_available = False
            key.cached_quota_available = None
            key.cached_capacity_score = None
            key.last_refreshed_at = now
            self._merge_refresh_signals_into_status(key, now)
            return None

        try:
            key.cached_available = await plugin.is_credential_available(probe_credential)
        except Exception as exc:
            logger.warning("is_credential_available failed for %s: %s", key.id, exc)
            key.cached_available = False

        if key.cached_available is False:
            self._clear_supported_models_on_failure(key)
            key.cached_quota_available = None
            key.cached_capacity_score = None
            key.last_refreshed_at = now
            self._merge_refresh_signals_into_status(key, now)
            return None

        try:
            signal = await plugin.get_capacity_signal(probe_credential)
        except Exception as exc:
            logger.warning("get_capacity_signal failed for %s: %s", key.id, exc)
            signal = None

        key.cached_quota_available = None if signal is None else signal.quota_available
        key.cached_capacity_score = None if signal is None else signal.capacity_score
        key.last_refreshed_at = now
        self._merge_refresh_signals_into_status(key, now)
        return probe_credential

    async def _prepare_registration_credential(self, credential: dict[str, Any], plugin) -> dict[str, Any]:
        try:
            result = await plugin.prepare_credential(credential)
        except ValueError as exc:
            raise InvalidCredentialError(str(exc)) from exc
        return result.credential

    async def _merge_runtime_key_for_persist(self, key: ApiKey) -> ApiKey | None:
        latest = await self._repository.get_key(key.id)
        if latest is None:
            return None
        self._merge_runtime_mutation(latest, key)
        return latest

    async def _persist_runtime_key(
        self,
        key: ApiKey,
        now: datetime,
        owner: str,
    ) -> ApiKey | None:
        merged = await self._merge_runtime_key_for_persist(key)
        if merged is None:
            return None
        return await self._repository.update_runtime_snapshot_if_locked(
            merged,
            owner,
            now,
        )

    async def _persist_background_runtime_key(
        self,
        key: ApiKey,
        now: datetime,
        owner: str,
    ) -> ApiKey | None:
        merged = await self._merge_runtime_key_for_persist(key)
        if merged is None:
            return None
        return await self._repository.update_background_runtime_snapshot_if_locked(
            merged,
            owner,
            now,
        )

    @staticmethod
    def _merge_runtime_mutation(target: ApiKey, source: ApiKey) -> None:
        target.credential = source.credential
        if target.status not in {KeyStatus.DISABLED_ADMIN, KeyStatus.DISABLED_REPORT}:
            target.status = source.status
        target.cooldown_until = source.cooldown_until
        target.supported_models = list(source.supported_models)
        target.last_refreshed_at = source.last_refreshed_at
        target.cached_available = source.cached_available
        target.cached_quota_available = source.cached_quota_available
        target.cached_capacity_score = source.cached_capacity_score

    @staticmethod
    def _clear_supported_models_on_failure(key: ApiKey) -> None:
        if key.status in {KeyStatus.DISABLED_ADMIN, KeyStatus.DISABLED_REPORT}:
            return
        key.supported_models = []

    async def _run_key_preflight(self, key: ApiKey, plugin) -> dict[str, Any] | None:
        auth_type = str(getattr(plugin, "auth_type", "") or "").lower()
        if "oauth" not in auth_type:
            return key.credential

        try:
            fresh = plugin._is_oauth_credential_fresh(key.credential)
        except Exception as exc:
            logger.warning("oauth credential freshness check failed for %s: %s", key.id, exc)
            return None

        if not fresh:
            try:
                refreshed = await plugin._refresh_oauth_credential(key.credential)
            except Exception as exc:
                logger.warning("oauth credential refresh failed for %s: %s", key.id, exc)
                refreshed = None
            if refreshed is None:
                return None
            if not self._credential_equals(refreshed, key.credential):
                key.credential = refreshed

        try:
            return await self._build_runtime_credential_for_refresh(key.credential, plugin)
        except Exception as exc:
            logger.warning("build probe credential failed for %s: %s", key.id, exc)
            return None

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

        if key.status == KeyStatus.PENDING and key.cached_available is True:
            key.status = KeyStatus.AVAILABLE

        self._state_machine.recover_if_ready(key, now)

    def _is_fresh_pending(self, key: ApiKey, now: datetime) -> bool:
        if key.status != KeyStatus.PENDING:
            return False
        if key.last_refreshed_at is None:
            return True
        age_seconds = max((now - key.last_refreshed_at).total_seconds(), 0.0)
        return age_seconds < self._pending_validation_grace_seconds

    async def _get_required_key(self, key_id: str) -> ApiKey:
        key = await self._repository.get_key(key_id)
        if key is None:
            raise KeyNotFoundError(f"key {key_id} not found")
        return key

    async def _sync_models(self, key: ApiKey, plugin=None, credential: dict[str, Any] | None = None) -> None:
        """Fetch and store provider model list. Failures do not change status."""
        plugin = plugin or self._provider_registry.get(key.provider)
        if plugin is None:
            return
        try:
            key.supported_models = await plugin.fetch_models(credential or key.credential)
        except Exception as exc:
            key.supported_models = []
            logger.warning("fetch_models failed for %s: %s", key.id, exc)

    @staticmethod
    async def _build_runtime_credential_for_refresh(credential: dict[str, Any], plugin) -> dict[str, Any]:
        builder = getattr(plugin, "_build_runtime_credential", None)
        if callable(builder):
            return await builder(credential)
        return credential

    def _require_ready_provider(self, provider: str):
        plugin = self._provider_registry.get(provider)
        if plugin is None:
            raise ProviderNotFoundError(f"provider {provider} is not registered")
        if not plugin.is_plugin_ready():
            raise ProviderNotReadyError(f"provider {provider} is not ready")
        return plugin

    @staticmethod
    def _credential_equals(left: dict[str, Any], right: dict[str, Any]) -> bool:
        return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
