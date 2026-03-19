from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from domain.entities.api_key import ApiKey
from domain.exceptions.domain_exceptions import KeyNotFoundError, NoAvailableKeyError
from domain.repositories.key_repository import KeyAllocationStore, KeyRepository
from domain.services.scheduler import KeyScheduler
from domain.services.scorer import KeyScorer
from domain.services.state_machine import KeyStateMachine
from domain.value_objects.key_status import KeyStatus
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


class KeyService:
    def __init__(
        self,
        repository: KeyRepository,
        allocation_store: KeyAllocationStore,
        scheduler: KeyScheduler,
        scorer: KeyScorer,
        state_machine: KeyStateMachine,
        provider_registry: ProviderRegistry,
        allocation_lease_seconds: int = 2,
    ) -> None:
        self._repository = repository
        self._allocation_store = allocation_store
        self._scheduler = scheduler
        self._scorer = scorer
        self._state_machine = state_machine
        self._provider_registry = provider_registry
        self._allocation_lease_seconds = max(allocation_lease_seconds, 1)

    async def create_key(self, data: CreateKeyInput) -> ApiKey:
        now = utcnow()
        key = ApiKey(
            id=str(uuid4()),
            provider=data.provider.strip().lower(),
            credential=data.credential,
        )
        await self._sync_models(key)
        await self._repository.upsert_key(key)
        await self._allocation_store.sync_key(key, self._scorer.score(key, now))
        return key

    async def update_key(self, key_id: str, data: UpdateKeyInput) -> ApiKey:
        key = await self._get_required_key(key_id)
        if data.credential is not None:
            key.credential = data.credential
            await self._sync_models(key)
        if data.status is not None:
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

    async def allocate_key(self, provider: str, model: str | None = None) -> ApiKey:
        now = utcnow()
        provider = provider.strip().lower()
        keys = await self._repository.list_provider_keys(provider)
        await self._recover_ready_keys(keys, now)
        candidates, capacity_by_key_id = await self._collect_candidates(keys, model, now)

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

        return await self._finalize_allocation(ranked, allocated_id, now)

    async def allocate_key_by_model(self, model: str) -> ApiKey:
        now = utcnow()
        keys = await self._repository.list_keys()
        await self._recover_ready_keys(keys, now)
        candidates, capacity_by_key_id = await self._collect_candidates(keys, model, now)

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

        return await self._finalize_allocation(ranked, allocated_id, now)

    async def _recover_ready_keys(self, keys: list[ApiKey], now: datetime) -> None:
        for key in keys:
            before = key.status
            self._state_machine.recover_if_ready(key, now)
            if before != key.status:
                await self._repository.upsert_key(key)

    async def _collect_candidates(
        self,
        keys: list[ApiKey],
        model: str | None,
        now: datetime,
    ) -> tuple[list[ApiKey], dict[str, float | None]]:
        candidates: list[ApiKey] = []
        capacity_by_key_id: dict[str, float | None] = {}

        for key in keys:
            if not key.is_available(now):
                continue
            if model and key.supported_models and model not in key.supported_models:
                continue

            plugin = self._provider_registry.get(key.provider)
            if plugin is not None:
                try:
                    available = await plugin.is_credential_available(key.credential, model)
                except Exception as exc:
                    logger.warning("is_credential_available raised for %s: %s", key.id, exc)
                    available = False
                if not available:
                    continue
                try:
                    signal = await plugin.get_capacity_signal(key.credential)
                except Exception as exc:
                    logger.warning("get_capacity_signal raised for %s: %s", key.id, exc)
                    signal = None
                capacity_by_key_id[key.id] = None if signal is None else signal.capacity_score
            else:
                capacity_by_key_id[key.id] = None

            candidates.append(key)

        return candidates, capacity_by_key_id

    async def _finalize_allocation(
        self,
        ranked: list,
        allocated_id: str,
        now: datetime,
    ) -> ApiKey:
        selected = next((item.key for item in ranked if item.key.id == allocated_id), None)
        if selected is None:
            selected = await self._get_required_key(allocated_id)

        selected.mark_used(now)
        await self._repository.upsert_key(selected)
        await self._allocation_store.sync_key(selected, self._scorer.score(selected, now))
        return selected

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

    async def _get_required_key(self, key_id: str) -> ApiKey:
        key = await self._repository.get_key(key_id)
        if key is None:
            raise KeyNotFoundError(f"key {key_id} not found")
        return key

    async def _sync_models(self, key: ApiKey) -> None:
        """Fetch and store the supported model list from the plugin."""
        plugin = self._provider_registry.get(key.provider)
        if plugin is None:
            return
        try:
            key.supported_models = await plugin.fetch_models(key.credential)
        except Exception as exc:
            logger.warning("fetch_models failed for %s: %s", key.id, exc)
