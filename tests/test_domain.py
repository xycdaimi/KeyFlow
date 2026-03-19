from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from application.services.key_service import CreateKeyInput, KeyService, UpdateKeyInput
from domain.entities.api_key import ApiKey
from domain.exceptions.domain_exceptions import DuplicateCredentialError
from domain.services.scheduler import KeyScheduler
from domain.services.scorer import KeyScorer, ScoreWeights
from domain.services.state_machine import KeyStateMachine
from domain.value_objects.key_status import KeyStatus
from infrastructure.plugins.base import CapacitySignal
from tests.fakes import (
    FakeProviderPlugin,
    InMemoryAllocationStore,
    InMemoryAnyProviderAllocationStore,
    InMemoryKeyRepository,
    build_provider_registry,
)


def test_scorer_prefers_healthier_key() -> None:
    now = datetime.now(timezone.utc)
    scorer = KeyScorer(ScoreWeights())

    healthy = ApiKey(
        id="healthy",
        provider="openai",
        credential={"api_key": "sk-1"},
        success_count=20,
        error_count=1,
        last_used_at=now - timedelta(minutes=5),
    )
    weak = ApiKey(
        id="weak",
        provider="openai",
        credential={"api_key": "sk-2"},
        success_count=1,
        error_count=8,
        last_used_at=now - timedelta(seconds=5),
    )

    assert scorer.score(healthy, now) > scorer.score(weak, now)


def test_state_machine_applies_backoff_and_recovery() -> None:
    now = datetime.now(timezone.utc)
    key = ApiKey(id="k1", provider="openai", credential={"api_key": "sk-test"})
    machine = KeyStateMachine(rate_limit_backoff_minutes=(1, 2, 5))

    machine.on_error(key, "rate_limit", now)

    assert key.status == KeyStatus.RATE_LIMITED
    assert key.cooldown_until == now + timedelta(minutes=1)

    machine.recover_if_ready(key, now + timedelta(minutes=2))
    assert key.status == KeyStatus.AVAILABLE
    assert key.cooldown_until is None


def test_state_machine_handles_generic_error_without_exhaustion_probe() -> None:
    now = datetime.now(timezone.utc)
    key = ApiKey(id="k-generic", provider="openai", credential={"api_key": "sk-test"})
    machine = KeyStateMachine()

    machine.on_error(key, "network_timeout", now)

    assert key.status == KeyStatus.AVAILABLE
    assert key.error_count == 1


def test_scheduler_selects_highest_ranked_key() -> None:
    now = datetime.now(timezone.utc)
    scheduler = KeyScheduler(KeyScorer(), jitter=0.0)

    better = ApiKey(
        id="better",
        provider="openai",
        credential={"api_key": "sk-1"},
        success_count=10,
        last_used_at=now - timedelta(minutes=10),
    )
    worse = ApiKey(
        id="worse",
        provider="openai",
        credential={"api_key": "sk-2"},
        error_count=5,
        last_used_at=now - timedelta(seconds=10),
    )

    selected = scheduler.select_key([worse, better], now)
    assert selected.id == "better"


def test_scorer_prefers_key_with_higher_capacity_signal() -> None:
    now = datetime.now(timezone.utc)
    scorer = KeyScorer(ScoreWeights(capacity=0.4))

    key_a = ApiKey(id="a", provider="openrouter", credential={"api_key": "a"})
    key_b = ApiKey(id="b", provider="openrouter", credential={"api_key": "b"})

    assert scorer.score(key_a, now, capacity_score=0.8) > scorer.score(
        key_b, now, capacity_score=0.2
    )


def test_scorer_uses_neutral_fallback_when_capacity_missing() -> None:
    now = datetime.now(timezone.utc)
    scorer = KeyScorer(ScoreWeights(capacity=0.4, capacity_unknown_fallback=0.5))
    key = ApiKey(id="fallback", provider="openai", credential={"api_key": "sk-test"})

    assert scorer.score(key, now, capacity_score=None) == scorer.score(key, now, capacity_score=0.5)


def test_scheduler_uses_capacity_signal_as_runtime_tiebreaker() -> None:
    now = datetime.now(timezone.utc)
    scheduler = KeyScheduler(KeyScorer(ScoreWeights(capacity=0.4)), jitter=0.0)

    low = ApiKey(id="low", provider="openrouter", credential={"api_key": "low"})
    high = ApiKey(id="high", provider="openrouter", credential={"api_key": "high"})

    selected = scheduler.select_key(
        [low, high],
        now,
        capacity_by_key_id={"low": 0.1, "high": 0.9},
    )

    assert selected.id == "high"


@pytest.mark.anyio
async def test_service_allocate_by_model_prefers_best_key_across_providers() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="openai-low",
                provider="openai",
                credential={"api_key": "sk-openai"},
                supported_models=["gpt-4o"],
                last_used_at=now - timedelta(minutes=5),
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=0.1,
            ),
            ApiKey(
                id="openrouter-high",
                provider="openrouter",
                credential={"api_key": "sk-openrouter"},
                supported_models=["gpt-4o"],
                last_used_at=now - timedelta(minutes=5),
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=0.9,
            ),
        ]
    )
    allocation_store = InMemoryAnyProviderAllocationStore()
    scorer = KeyScorer()
    scheduler = KeyScheduler(scorer, jitter=0.0)
    state_machine = KeyStateMachine()
    provider_registry = build_provider_registry(
        FakeProviderPlugin(
            "openai",
            ["gpt-4o"],
            available=True,
            capacity_signal=CapacitySignal(
                has_capacity_signal=True,
                capacity_score=0.1,
                capacity_kind="remaining_budget_ratio",
                reason="low capacity",
            ),
        ),
        FakeProviderPlugin(
            "openrouter",
            ["gpt-4o"],
            available=True,
            capacity_signal=CapacitySignal(
                has_capacity_signal=True,
                capacity_score=0.9,
                capacity_kind="remaining_budget_ratio",
                reason="high capacity",
            ),
        ),
    )
    service = KeyService(repository, allocation_store, scheduler, scorer, state_machine, provider_registry)

    selected = await service.allocate_key_by_model("gpt-4o")

    assert selected.id == "openrouter-high"
    assert allocation_store.any_provider_ordered_ids == ["openrouter-high", "openai-low"]


@pytest.mark.anyio
async def test_service_allocate_by_model_excludes_keys_without_target_model_support() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="openai-unsupported",
                provider="openai",
                credential={"api_key": "sk-openai"},
                supported_models=["gpt-3.5-turbo"],
                last_used_at=now - timedelta(minutes=5),
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=0.9,
            ),
            ApiKey(
                id="anthropic-supported",
                provider="anthropic",
                credential={"api_key": "sk-anthropic"},
                supported_models=["gpt-4o"],
                last_used_at=now - timedelta(minutes=5),
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=0.1,
            ),
        ]
    )
    allocation_store = InMemoryAnyProviderAllocationStore()
    scorer = KeyScorer()
    scheduler = KeyScheduler(scorer, jitter=0.0)
    state_machine = KeyStateMachine()
    provider_registry = build_provider_registry(
        FakeProviderPlugin(
            "openai",
            ["gpt-3.5-turbo"],
            available=True,
            capacity_signal=CapacitySignal(
                has_capacity_signal=True,
                capacity_score=0.9,
                capacity_kind="remaining_budget_ratio",
                reason="would win if not filtered",
            ),
        ),
        FakeProviderPlugin(
            "anthropic",
            ["gpt-4o"],
            available=True,
            capacity_signal=CapacitySignal(
                has_capacity_signal=True,
                capacity_score=0.1,
                capacity_kind="remaining_budget_ratio",
                reason="supported target model",
            ),
        ),
    )
    service = KeyService(repository, allocation_store, scheduler, scorer, state_machine, provider_registry)

    selected = await service.allocate_key_by_model("gpt-4o")

    assert selected.id == "anthropic-supported"
    assert allocation_store.any_provider_ordered_ids == ["anthropic-supported"]


@pytest.mark.anyio
async def test_recover_ready_keys_only_persists_keys_that_changed_status() -> None:
    now = datetime.now(timezone.utc)

    class SpyKeyRepository(InMemoryKeyRepository):
        def __init__(self, keys: list[ApiKey]) -> None:
            super().__init__(keys)
            self.upserted_ids: list[str] = []

        async def upsert_key(self, key: ApiKey) -> ApiKey:
            self.upserted_ids.append(key.id)
            return await super().upsert_key(key)

        async def list_provider_keys(self, provider: str) -> list[ApiKey]:
            return [deepcopy(key) for key in await super().list_provider_keys(provider)]

    repository = SpyKeyRepository(
        [
            ApiKey(
                id="already-available",
                provider="openai",
                credential={"api_key": "sk-ready"},
                status=KeyStatus.AVAILABLE,
            ),
            ApiKey(
                id="recoverable",
                provider="openai",
                credential={"api_key": "sk-recover"},
                status=KeyStatus.RATE_LIMITED,
                cooldown_until=now - timedelta(seconds=1),
            ),
            ApiKey(
                id="still-cooling",
                provider="openai",
                credential={"api_key": "sk-cooling"},
                status=KeyStatus.RATE_LIMITED,
                cooldown_until=now + timedelta(seconds=30),
            ),
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(FakeProviderPlugin("openai", ["gpt-4o"], available=True)),
    )

    keys = await repository.list_provider_keys("openai")
    await service._recover_ready_keys(keys, now)

    assert repository.upserted_ids == ["recoverable", "still-cooling"]
    assert repository._keys["already-available"].status == KeyStatus.AVAILABLE
    assert repository._keys["recoverable"].status == KeyStatus.AVAILABLE
    assert repository._keys["recoverable"].cooldown_until is None
    assert repository._keys["still-cooling"].status == KeyStatus.COOLDOWN


@pytest.mark.anyio
async def test_create_key_rejects_duplicate_credential_within_same_provider() -> None:
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="existing-openai",
                provider="openai",
                credential={"api_key": "sk-same"},
            ),
            ApiKey(
                id="existing-openrouter",
                provider="openrouter",
                credential={"api_key": "sk-same"},
            ),
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(FakeProviderPlugin("openai", ["gpt-4o"], available=True)),
    )

    with pytest.raises(DuplicateCredentialError):
        await service.create_key(CreateKeyInput(provider="openai", credential={"api_key": "sk-same"}))


@pytest.mark.anyio
async def test_update_key_rejects_duplicate_credential_within_same_provider() -> None:
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-a",
                provider="openai",
                credential={"api_key": "sk-a"},
            ),
            ApiKey(
                id="key-b",
                provider="openai",
                credential={"api_key": "sk-b"},
            ),
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(FakeProviderPlugin("openai", ["gpt-4o"], available=True)),
    )

    with pytest.raises(DuplicateCredentialError):
        await service.update_key("key-b", UpdateKeyInput(credential={"api_key": "sk-a"}))


@pytest.mark.anyio
async def test_in_memory_allocation_store_skips_removed_keys() -> None:
    store = InMemoryAllocationStore()
    now = datetime.now(timezone.utc)

    await store.remove_key("key-1", "openai")

    allocated = await store.allocate_key("openai", ["key-1", "key-2"], now)

    assert allocated == "key-2"
    assert store.allocate_calls == [("openai", ["key-1", "key-2"])]


@pytest.mark.anyio
async def test_in_memory_allocation_store_honors_active_lease_until_release() -> None:
    store = InMemoryAllocationStore()
    now = datetime.now(timezone.utc)

    first = await store.allocate_key("openai", ["key-1", "key-2"], now)
    second = await store.allocate_key("openai", ["key-1", "key-2"], now)
    await store.release_key_lease("openai", "key-1")
    third = await store.allocate_key("openai", ["key-1", "key-2"], now)

    assert first == "key-1"
    assert second == "key-2"
    assert third == "key-1"
    assert store.released == [("openai", "key-1")]


@pytest.mark.anyio
async def test_in_memory_allocation_store_reuses_key_after_lease_expires() -> None:
    store = InMemoryAllocationStore()
    now = datetime.now(timezone.utc)

    first = await store.allocate_key("openai", ["key-1", "key-2"], now, lease_seconds=2)
    second = await store.allocate_key("openai", ["key-1", "key-2"], now + timedelta(seconds=1), lease_seconds=2)
    third = await store.allocate_key("openai", ["key-1", "key-2"], now + timedelta(seconds=3), lease_seconds=2)

    assert first == "key-1"
    assert second == "key-2"
    assert third == "key-1"
