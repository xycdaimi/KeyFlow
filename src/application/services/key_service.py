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
    """Minimum input to add a new api-key account.

    api_key is the account. No display_name, no quota_total.
    """

    provider: str
    api_key: str


@dataclass(slots=True)
class UpdateKeyInput:
    """Fields that can be changed by the caller."""

    api_key: str | None = None
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
            api_key=data.api_key,
        )
        await self._sync_models(key)
        await self._repository.upsert_key(key)
        await self._allocation_store.sync_key(key, self._scorer.score(key, now))
        return key

    async def update_key(self, key_id: str, data: UpdateKeyInput) -> ApiKey:
        key = await self._get_required_key(key_id)
        if data.api_key is not None:
            key.api_key = data.api_key
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
            return await plugin.explain_credential(key.api_key)
        except Exception as exc:
            logger.warning("explain_credential failed for %s: %s", key.id, exc)
            return {"provider": key.provider, "status": "plugin_error", "error_code": "explain_failed"}

    async def allocate_key(self, provider: str, model: str | None = None) -> ApiKey:
        now = utcnow()
        provider = provider.strip().lower()
        keys = await self._repository.list_provider_keys(provider)

        for key in keys:
            self._state_machine.recover_if_ready(key, now)
            if key.status == KeyStatus.AVAILABLE:
                await self._repository.upsert_key(key)

        plugin = self._provider_registry.get(provider)

        # Filter: core state + plugin availability signal
        candidates: list[ApiKey] = []
        for key in keys:
            if not key.is_available(now):
                continue
            # Local pre-filter when model capability was synced before.
            if model and key.supported_models and model not in key.supported_models:
                continue
            if plugin is not None:
                try:
                    available = await plugin.is_credential_available(key.api_key, model)
                except Exception as exc:
                    logger.warning("is_credential_available raised for %s: %s", key.id, exc)
                    available = False
                if not available:
                    continue
            candidates.append(key)

        ranked = self._scheduler.rank_keys(candidates, now)
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
                await plugin.mark_success(key.api_key, {"tokens_used": tokens_used})
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
                await plugin.mark_error(key.api_key, {"error_type": error_type})
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
            key.supported_models = await plugin.fetch_models(key.api_key)
        except Exception as exc:
            logger.warning("fetch_models failed for %s: %s", key.id, exc)
