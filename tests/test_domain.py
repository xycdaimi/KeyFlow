"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-23
@Description: 领域服务与分配逻辑回归测试
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from application.services.key_service import CreateKeyInput, KeyService, UpdateKeyInput
from application.services.model_alias_resolver import ModelAliasResolver
from domain.entities.api_key import ApiKey
from domain.exceptions.domain_exceptions import (
    DuplicateCredentialError,
    KeyNotFoundError,
    NoAvailableKeyError,
    ProviderNotFoundError,
    ProviderNotReadyError,
    UpstreamUnreachableError,
)
from domain.services.scheduler import KeyScheduler
from domain.services.scorer import KeyScorer, ScoreWeights
from domain.services.state_machine import KeyStateMachine
from domain.value_objects.key_status import KeyStatus
from infrastructure.plugins.base import CapacitySignal, CredentialPreparationResult
from infrastructure.plugins.providers.gemini_oauth import GeminiOauthPlugin
from tests.fakes import (
    FakeOauthProviderPlugin,
    FakeProviderPlugin,
    InMemoryAllocationStore,
    InMemoryAnyProviderAllocationStore,
    InMemoryKeyRepository,
    build_provider_registry,
)

MODEL_ALIAS_VALID_PATH = Path("tests/output/model_alias/valid_with_aliases.yaml")


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


def test_state_machine_error_feedback_does_not_change_status() -> None:
    now = datetime.now(timezone.utc)
    key = ApiKey(id="k1", provider="openai", credential={"api_key": "sk-test"})
    machine = KeyStateMachine(rate_limit_backoff_minutes=(1, 2, 5))

    machine.on_error(key, "rate_limit", now)

    assert key.status == KeyStatus.AVAILABLE
    assert key.cooldown_until is None
    assert key.error_count == 1


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


def test_scheduler_prefers_fresh_key_over_equivalent_stale_key() -> None:
    now = datetime.now(timezone.utc)
    scorer = KeyScorer()
    scheduler = KeyScheduler(scorer, jitter=0.0)
    fresh = ApiKey(
        id="fresh",
        provider="openai",
        credential={"api_key": "sk-fresh"},
        last_used_at=now - timedelta(minutes=5),
        last_refreshed_at=now,
    )
    stale = ApiKey(
        id="stale",
        provider="openai",
        credential={"api_key": "sk-stale"},
        last_used_at=now - timedelta(minutes=5),
        last_refreshed_at=now - timedelta(seconds=120),
    )

    ranked = scheduler.rank_keys(
        [stale, fresh],
        now,
        capacity_by_key_id={"fresh": 0.7, "stale": 0.7},
    )

    assert [item.key.id for item in ranked] == ["fresh", "stale"]


def test_scheduler_prefers_stale_key_over_very_stale_key_when_other_signals_match() -> None:
    now = datetime.now(timezone.utc)
    scorer = KeyScorer()
    scheduler = KeyScheduler(scorer, jitter=0.0)
    stale = ApiKey(
        id="stale",
        provider="openai",
        credential={"api_key": "sk-stale"},
        last_used_at=now - timedelta(minutes=5),
        last_refreshed_at=now - timedelta(seconds=120),
    )
    very_stale = ApiKey(
        id="very-stale",
        provider="openai",
        credential={"api_key": "sk-very-stale"},
        last_used_at=now - timedelta(minutes=5),
        last_refreshed_at=now - timedelta(seconds=240),
    )

    ranked = scheduler.rank_keys(
        [very_stale, stale],
        now,
        capacity_by_key_id={"stale": 0.7, "very-stale": 0.7},
    )

    assert [item.key.id for item in ranked] == ["stale", "very-stale"]


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

    assert selected.key.id == "openrouter-high"
    assert selected.provider_model == "gpt-4o"
    assert allocation_store.any_provider_ordered_ids == ["openrouter-high", "openai-low"]


@pytest.mark.anyio
async def test_allocate_key_includes_stale_supported_key_in_ranked_candidates() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="fresh-key",
                provider="openai",
                credential={"api_key": "sk-fresh"},
                supported_models=["gpt-4o"],
                last_used_at=now - timedelta(minutes=5),
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=0.6,
            ),
            ApiKey(
                id="stale-key",
                provider="openai",
                credential={"api_key": "sk-stale"},
                supported_models=["gpt-4o"],
                last_used_at=now - timedelta(minutes=5),
                last_refreshed_at=now - timedelta(seconds=120),
                cached_available=True,
                cached_capacity_score=0.6,
            ),
        ]
    )
    allocation_store = InMemoryAllocationStore()
    scorer = KeyScorer()
    scheduler = KeyScheduler(scorer, jitter=0.0)
    service = KeyService(
        repository,
        allocation_store,
        scheduler,
        scorer,
        KeyStateMachine(),
        build_provider_registry(FakeProviderPlugin("openai", ["gpt-4o"], available=True)),
        refresh_cache_seconds=60,
    )

    await service.allocate_key("openai", "gpt-4o")

    assert allocation_store.allocate_calls == [("openai", ["fresh-key", "stale-key"])]


@pytest.mark.anyio
async def test_allocate_by_model_allows_stale_key_when_it_is_the_only_allocatable_candidate() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="disabled-openai",
                provider="openai",
                credential={"api_key": "sk-disabled"},
                status=KeyStatus.DISABLED_ADMIN,
                supported_models=["gpt-4o"],
                last_refreshed_at=now,
                cached_available=True,
            ),
            ApiKey(
                id="stale-openrouter",
                provider="openrouter",
                credential={"api_key": "sk-stale"},
                status=KeyStatus.AVAILABLE,
                supported_models=["gpt-4o"],
                last_refreshed_at=now - timedelta(seconds=120),
                cached_available=True,
                cached_capacity_score=0.4,
            ),
        ]
    )
    scorer = KeyScorer()
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(scorer, jitter=0.0),
        scorer,
        KeyStateMachine(),
        build_provider_registry(
            FakeProviderPlugin("openai", ["gpt-4o"], available=True),
            FakeProviderPlugin("openrouter", ["gpt-4o"], available=True),
        ),
        refresh_cache_seconds=60,
    )

    selected = await service.allocate_key_by_model("gpt-4o")

    assert selected.key.id == "stale-openrouter"
    assert selected.provider_model == "gpt-4o"


@pytest.mark.anyio
async def test_allocate_key_tries_next_ranked_candidate_when_first_is_leased() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="fresh-first",
                provider="openai",
                credential={"api_key": "sk-first"},
                supported_models=["gpt-4o"],
                last_used_at=now - timedelta(minutes=5),
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=0.8,
            ),
            ApiKey(
                id="stale-second",
                provider="openai",
                credential={"api_key": "sk-second"},
                supported_models=["gpt-4o"],
                last_used_at=now - timedelta(minutes=5),
                last_refreshed_at=now - timedelta(seconds=120),
                cached_available=True,
                cached_capacity_score=0.7,
            ),
        ]
    )
    allocation_store = InMemoryAllocationStore()
    allocation_store.active_leases[("openai", "fresh-first")] = now + timedelta(seconds=10)
    scorer = KeyScorer()
    service = KeyService(
        repository,
        allocation_store,
        KeyScheduler(scorer, jitter=0.0),
        scorer,
        KeyStateMachine(),
        build_provider_registry(FakeProviderPlugin("openai", ["gpt-4o"], available=True)),
        refresh_cache_seconds=60,
    )

    selected = await service.allocate_key("openai", "gpt-4o")

    assert selected.key.id == "stale-second"


@pytest.mark.anyio
async def test_finalize_allocation_does_not_overwrite_newer_persisted_key_state() -> None:
    now = datetime.now(timezone.utc)
    stale_snapshot = ApiKey(
        id="key-1",
        provider="openai",
        credential={"api_key": "old-token"},
        status=KeyStatus.AVAILABLE,
        supported_models=["gpt-4o"],
        last_refreshed_at=now - timedelta(minutes=10),
        updated_at=now - timedelta(minutes=5),
    )
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "new-token"},
                status=KeyStatus.AVAILABLE,
                supported_models=["gpt-4o", "gpt-4o-mini"],
                last_refreshed_at=now,
                cached_available=True,
                updated_at=now,
            )
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
    ranked = [type("_Ranked", (), {"key": stale_snapshot})()]

    result = await service._finalize_allocation(
        ranked,
        "key-1",
        now,
        {"key-1": "gpt-4o"},
    )

    assert result.key.credential == {"api_key": "new-token"}
    assert repository._keys["key-1"].credential == {"api_key": "new-token"}
    assert repository._keys["key-1"].supported_models == ["gpt-4o", "gpt-4o-mini"]
    assert repository._keys["key-1"].last_used_at == now


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

    assert selected.key.id == "anthropic-supported"
    assert selected.provider_model == "gpt-4o"
    assert allocation_store.any_provider_ordered_ids == ["anthropic-supported"]


@pytest.mark.anyio
async def test_allocate_by_model_returns_provider_model_from_alias_mapping() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-openrouter",
                provider="openrouter",
                credential={"api_key": "sk-openrouter"},
                supported_models=["openai/gpt-4o"],
                last_used_at=now,
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=0.9,
            )
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(FakeProviderPlugin("openrouter", ["openai/gpt-4o"], available=True)),
        model_alias_resolver=ModelAliasResolver.from_yaml_file(str(MODEL_ALIAS_VALID_PATH)),
    )

    selected = await service.allocate_key_by_model("gpt-4o")

    assert selected.key.id == "key-openrouter"
    assert selected.provider_model == "openai/gpt-4o"


@pytest.mark.anyio
async def test_allocate_key_falls_back_to_requested_model_when_alias_not_configured() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-openai",
                provider="openai",
                credential={"api_key": "sk-openai"},
                supported_models=["gpt-4o-mini"],
                last_used_at=now,
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=0.9,
            )
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(FakeProviderPlugin("openai", ["gpt-4o-mini"], available=True)),
        model_alias_resolver=ModelAliasResolver.empty(),
    )

    selected = await service.allocate_key("openai", "gpt-4o-mini")

    assert selected.key.id == "key-openai"
    assert selected.provider_model == "gpt-4o-mini"


@pytest.mark.anyio
async def test_recover_ready_keys_only_persists_keys_that_changed_status() -> None:
    now = datetime.now(timezone.utc)

    class SpyKeyRepository(InMemoryKeyRepository):
        def __init__(self, keys: list[ApiKey]) -> None:
            super().__init__(keys)
            self.upserted_ids: list[str] = []
            self.updated_status_ids: list[str] = []

        async def upsert_key(self, key: ApiKey) -> ApiKey:
            self.upserted_ids.append(key.id)
            return await super().upsert_key(key)

        async def update_status(
            self,
            key_id: str,
            status: str,
            cooldown_until: datetime | None,
            now: datetime,
        ) -> ApiKey | None:
            self.updated_status_ids.append(key_id)
            return await super().update_status(key_id, status, cooldown_until, now)

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

    assert repository.upserted_ids == []
    assert repository.updated_status_ids == ["recoverable", "still-cooling"]
    assert repository._keys["already-available"].status == KeyStatus.AVAILABLE
    assert repository._keys["recoverable"].status == KeyStatus.AVAILABLE
    assert repository._keys["recoverable"].cooldown_until is None
    assert repository._keys["still-cooling"].status == KeyStatus.COOLDOWN


@pytest.mark.anyio
async def test_create_key_rejects_duplicate_credential_within_same_provider() -> None:
    class _NormalizingPlugin(FakeProviderPlugin):
        async def prepare_credential(self, credential: dict[str, str]) -> CredentialPreparationResult:
            return CredentialPreparationResult(
                credential={**credential, "type": "normalized"},
                changed=True,
            )

    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="existing-openai",
                provider="openai",
                credential={"api_key": "sk-same", "type": "normalized"},
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
        build_provider_registry(_NormalizingPlugin("openai", ["gpt-4o"], available=True)),
    )

    with pytest.raises(DuplicateCredentialError):
        await service.create_key(CreateKeyInput(provider="openai", credential={"api_key": "sk-same"}))


@pytest.mark.anyio
async def test_create_key_rejects_unknown_provider() -> None:
    service = KeyService(
        InMemoryKeyRepository(),
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(FakeProviderPlugin("openai", ["gpt-4o"], available=True)),
    )

    with pytest.raises(ProviderNotFoundError):
        await service.create_key(CreateKeyInput(provider="missing", credential={"api_key": "sk-missing"}))


@pytest.mark.anyio
async def test_create_key_rejects_provider_that_is_not_ready() -> None:
    service = KeyService(
        InMemoryKeyRepository(),
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(
            FakeProviderPlugin("gemini-web-proxy", ["gemini-2.5-pro"], available=True, plugin_ready=False)
        ),
    )

    with pytest.raises(ProviderNotReadyError):
        await service.create_key(
            CreateKeyInput(
                provider="gemini-web-proxy",
                credential={"secure_1psid": "a", "secure_1psidts": "b"},
            )
        )


@pytest.mark.anyio
async def test_create_key_skips_persist_when_upstream_root_unreachable() -> None:
    repository = InMemoryKeyRepository()
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(
            FakeProviderPlugin("openai", ["gpt-4o"], available=True, upstream_root_reachable=False)
        ),
    )

    with pytest.raises(UpstreamUnreachableError):
        await service.create_key(CreateKeyInput(provider="openai", credential={"api_key": "sk-new"}))

    assert repository._keys == {}


@pytest.mark.anyio
async def test_create_key_api_key_provider_runs_formal_three_steps() -> None:
    repository = InMemoryKeyRepository()
    plugin = FakeProviderPlugin(
        "openai",
        ["gpt-4o"],
        available=True,
        capacity_signal=CapacitySignal(
            has_capacity_signal=True,
            capacity_score=0.8,
            quota_available=True,
            capacity_kind="remaining_ratio",
            reason="fake",
        ),
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )
    service._schedule_pending_validation = lambda key_id: None

    key = await service.create_key(CreateKeyInput(provider="openai", credential={"api_key": "sk-new"}))

    assert plugin.available_checks == [{"api_key": "sk-new"}]
    assert key.cached_available is True
    assert key.cached_quota_available is True
    assert key.supported_models == ["gpt-4o"]


@pytest.mark.anyio
async def test_refresh_keys_oauth_provider_refreshes_first_and_persists_new_credential() -> None:
    plugin = FakeOauthProviderPlugin(
        "codex_oauth",
        ["gpt-5.4"],
        fresh=False,
        refreshed_credential={"access_token": "new-token", "expired": "2099-01-01T00:00:00Z"},
        available=True,
        capacity_signal=CapacitySignal(
            has_capacity_signal=True,
            capacity_score=0.5,
            quota_available=True,
            capacity_kind="remaining_ratio",
            reason="fake",
        ),
    )
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="codex_oauth",
                credential={"access_token": "old-token", "expired": "2000-01-01T00:00:00Z"},
                status=KeyStatus.DISABLED_UPSTREAM,
                last_refreshed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            )
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )

    await service.refresh_keys()

    assert repository._keys["key-1"].credential["access_token"] == "new-token"
    assert plugin.fresh_checks == [{"access_token": "old-token", "expired": "2000-01-01T00:00:00Z"}]
    assert plugin.refresh_calls == [{"access_token": "old-token", "expired": "2000-01-01T00:00:00Z"}]
    assert plugin.available_checks[0]["access_token"] == "new-token"


@pytest.mark.anyio
async def test_refresh_keys_oauth_provider_sets_disabled_upstream_when_refresh_fails() -> None:
    plugin = FakeOauthProviderPlugin(
        "gemini_oauth",
        ["gemini-2.5-pro"],
        fresh=False,
        refresh_result=None,
        available=True,
    )
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="gemini_oauth",
                credential={"refresh_token": "bad", "expiry_date": "1"},
                status=KeyStatus.AVAILABLE,
                supported_models=["gemini-2.5-pro"],
                last_refreshed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            )
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )

    await service.refresh_keys()

    assert repository._keys["key-1"].status == KeyStatus.DISABLED_UPSTREAM
    assert repository._keys["key-1"].supported_models == []
    assert plugin.available_checks == []


@pytest.mark.anyio
async def test_refresh_keys_oauth_refresh_failure_skips_capacity_and_model_sync() -> None:
    plugin = FakeOauthProviderPlugin(
        "codex_oauth",
        ["gpt-5.4"],
        fresh=False,
        refresh_result=None,
        available=True,
        capacity_signal=CapacitySignal(
            has_capacity_signal=True,
            capacity_score=0.4,
            quota_available=True,
            capacity_kind="remaining_ratio",
            reason="fake",
        ),
    )
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="codex_oauth",
                credential={"expired": "1"},
                status=KeyStatus.AVAILABLE,
                last_refreshed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            )
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )

    await service.refresh_keys()

    assert repository._keys["key-1"].cached_available is False
    assert repository._keys["key-1"].supported_models == []
    assert plugin.available_checks == []


@pytest.mark.anyio
async def test_refresh_keys_reuses_gemini_oauth_runtime_project_without_persisting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        def __init__(self, payload: dict, status_code: int = 200):
            self._payload = payload
            self.status_code = status_code
            self.is_success = 200 <= status_code < 300

        def raise_for_status(self) -> None:
            if not self.is_success:
                raise RuntimeError("http error")

        def json(self) -> dict:
            return self._payload

    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *args, **kwargs) -> _FakeResponse:
            self.calls.append(url)
            if url.endswith(":loadCodeAssist"):
                return _FakeResponse({"cloudaicompanionProject": "project-123"})
            if url.endswith(":retrieveUserQuota"):
                return _FakeResponse(
                    {
                        "buckets": [
                            {"modelId": "gemini-2.5-pro", "remainingFraction": 0.8},
                            {"modelId": "gemini-2.5-flash", "remainingFraction": 0.4},
                        ]
                    }
                )
            raise AssertionError(f"unexpected url: {url}")

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "infrastructure.plugins.providers.gemini_oauth.httpx.AsyncClient",
        lambda **kwargs: fake_client,
    )

    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="gemini_oauth",
                credential={
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expiry_date": "9999999999999",
                },
                status=KeyStatus.AVAILABLE,
                last_refreshed_at=datetime.now(timezone.utc) - timedelta(minutes=10),
            )
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(GeminiOauthPlugin()),
    )

    await service.refresh_keys()

    assert fake_client.calls.count("https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist") == 1
    assert fake_client.calls.count("https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota") == 3
    assert "project_id" not in repository._keys["key-1"].credential
    assert repository._keys["key-1"].supported_models == ["gemini-2.5-flash", "gemini-2.5-pro"]


@pytest.mark.anyio
async def test_create_key_oauth_refresh_failure_persists_disabled_upstream() -> None:
    repository = InMemoryKeyRepository()
    plugin = FakeOauthProviderPlugin(
        "gemini_oauth",
        ["gemini-2.5-pro"],
        fresh=False,
        refresh_result=None,
        available=True,
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )
    service._schedule_pending_validation = lambda key_id: None

    key = await service.create_key(
        CreateKeyInput(
            provider="gemini_oauth",
            credential={"refresh_token": "bad", "expiry_date": "1"},
        )
    )

    assert key.status == KeyStatus.DISABLED_UPSTREAM
    assert repository._keys[key.id].status == KeyStatus.DISABLED_UPSTREAM
    assert plugin.available_checks == []


@pytest.mark.anyio
async def test_validate_pending_key_runs_model_sync_after_status_probe() -> None:
    plugin = FakeProviderPlugin(
        "openai",
        ["gpt-4o", "gpt-4.1"],
        available=True,
        capacity_signal=CapacitySignal(
            has_capacity_signal=True,
            capacity_score=0.9,
            quota_available=True,
            capacity_kind="remaining_ratio",
            reason="fake",
        ),
    )
    repository = InMemoryKeyRepository(
        [ApiKey(id="key-1", provider="openai", credential={"api_key": "sk-test"}, status=KeyStatus.PENDING)]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )

    assert await service.validate_pending_key("key-1") is True
    assert repository._keys["key-1"].supported_models == ["gpt-4o", "gpt-4.1"]


@pytest.mark.anyio
async def test_create_key_sets_disabled_upstream_when_initial_availability_probe_fails() -> None:
    repository = InMemoryKeyRepository()
    plugin = FakeProviderPlugin("openai", ["gpt-4o"], available=False)
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )
    service._schedule_pending_validation = lambda key_id: None

    key = await service.create_key(CreateKeyInput(provider="openai", credential={"api_key": "sk-new"}))

    assert key.status == KeyStatus.DISABLED_UPSTREAM
    assert repository._keys[key.id].status == KeyStatus.DISABLED_UPSTREAM
    assert plugin.available_checks == [{"api_key": "sk-new"}]


@pytest.mark.anyio
async def test_validate_pending_key_converges_to_available() -> None:
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.PENDING,
            )
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

    assert await service.validate_pending_key("key-1") is True
    assert repository._keys["key-1"].status == KeyStatus.AVAILABLE


@pytest.mark.anyio
async def test_pending_key_is_not_allocatable() -> None:
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.PENDING,
                supported_models=["gpt-4o"],
            )
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

    with pytest.raises(NoAvailableKeyError):
        await service.allocate_key("openai", "gpt-4o")


@pytest.mark.anyio
async def test_refresh_skips_fresh_pending_key() -> None:
    now = datetime.now(timezone.utc)
    plugin = FakeProviderPlugin("openai", ["gpt-4o"], available=True)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.PENDING,
                last_refreshed_at=now,
            )
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )

    assert await service.refresh_keys() == 0
    assert repository._keys["key-1"].status == KeyStatus.PENDING
    assert plugin.available_checks == []


@pytest.mark.anyio
async def test_refresh_takes_over_stale_pending_key() -> None:
    now = datetime.now(timezone.utc) - timedelta(seconds=70)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.PENDING,
                last_refreshed_at=now,
            )
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

    assert await service.refresh_keys() == 1
    assert repository._keys["key-1"].status == KeyStatus.AVAILABLE


@pytest.mark.anyio
async def test_update_key_skips_credential_change_when_upstream_root_unreachable() -> None:
    repository = InMemoryKeyRepository(
        [
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
        build_provider_registry(
            FakeProviderPlugin("openai", ["gpt-4o"], available=True, upstream_root_reachable=False)
        ),
    )

    with pytest.raises(UpstreamUnreachableError):
        await service.update_key("key-b", UpdateKeyInput(credential={"api_key": "sk-new"}))

    assert repository._keys["key-b"].credential == {"api_key": "sk-b"}


@pytest.mark.anyio
async def test_update_key_rejects_duplicate_credential_within_same_provider() -> None:
    class _NormalizingPlugin(FakeProviderPlugin):
        async def prepare_credential(self, credential: dict[str, str]) -> CredentialPreparationResult:
            return CredentialPreparationResult(
                credential={**credential, "type": "normalized"},
                changed=True,
            )

    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-a",
                provider="openai",
                credential={"api_key": "sk-a", "type": "normalized"},
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
        build_provider_registry(_NormalizingPlugin("openai", ["gpt-4o"], available=True)),
    )

    with pytest.raises(DuplicateCredentialError):
        await service.update_key("key-b", UpdateKeyInput(credential={"api_key": "sk-a"}))


@pytest.mark.anyio
async def test_update_key_rejects_non_admin_writable_status() -> None:
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-a"},
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

    with pytest.raises(ValueError):
        await service.update_key("key-1", UpdateKeyInput(status=KeyStatus.DISABLED_REPORT))


@pytest.mark.anyio
async def test_update_key_restores_disabled_admin_to_available() -> None:
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.DISABLED_ADMIN,
                supported_models=["stale-model"],
            )
        ]
    )
    plugin = FakeProviderPlugin(
        "openai",
        ["gpt-4o", "gpt-4o-mini"],
        available=True,
        capacity_signal=CapacitySignal(
            has_capacity_signal=True,
            capacity_score=0.8,
            quota_available=True,
            capacity_kind="remaining_ratio",
            reason="healthy",
        ),
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )

    updated = await service.update_key("key-1", UpdateKeyInput(status=KeyStatus.AVAILABLE))

    assert updated.status == KeyStatus.AVAILABLE
    assert updated.supported_models == ["gpt-4o", "gpt-4o-mini"]


@pytest.mark.anyio
async def test_update_key_restores_disabled_admin_to_exhausted() -> None:
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openrouter",
                credential={"api_key": "sk-test"},
                status=KeyStatus.DISABLED_ADMIN,
                supported_models=["stale-model"],
            )
        ]
    )
    plugin = FakeProviderPlugin(
        "openrouter",
        ["gpt-4o"],
        available=True,
        capacity_signal=CapacitySignal(
            has_capacity_signal=True,
            capacity_score=0.0,
            quota_available=False,
            capacity_kind="remaining_budget_ratio",
            reason="exhausted",
        ),
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )

    updated = await service.update_key("key-1", UpdateKeyInput(status=KeyStatus.AVAILABLE))

    assert updated.status == KeyStatus.EXHAUSTED
    assert updated.supported_models == ["gpt-4o"]


@pytest.mark.anyio
async def test_update_key_restores_disabled_admin_to_disabled_upstream_and_clears_models() -> None:
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.DISABLED_ADMIN,
                supported_models=["stale-model"],
            )
        ]
    )
    plugin = FakeProviderPlugin("openai", ["gpt-4o"], available=False)
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )

    updated = await service.update_key("key-1", UpdateKeyInput(status=KeyStatus.AVAILABLE))

    assert updated.status == KeyStatus.DISABLED_UPSTREAM
    assert updated.supported_models == []


@pytest.mark.anyio
async def test_update_key_clears_models_but_keeps_status_when_fetch_models_fails() -> None:
    class _FetchModelsFailPlugin(FakeProviderPlugin):
        async def fetch_models(self, credential: dict[str, str]) -> list[str]:
            raise RuntimeError("models unavailable")

    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-old"},
                supported_models=["gpt-4o", "gpt-4o-mini"],
                status=KeyStatus.AVAILABLE,
            )
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(_FetchModelsFailPlugin("openai", ["gpt-4o"], available=True)),
    )

    updated = await service.update_key("key-1", UpdateKeyInput(credential={"api_key": "sk-new"}))

    assert updated.supported_models == []
    assert updated.status == KeyStatus.AVAILABLE
    assert repository._keys["key-1"].supported_models == []
    assert repository._keys["key-1"].status == KeyStatus.AVAILABLE


@pytest.mark.anyio
async def test_update_key_persists_prepared_registration_credential_before_status_refresh() -> None:
    class _NormalizingPlugin(FakeProviderPlugin):
        async def prepare_credential(self, credential: dict[str, str]) -> CredentialPreparationResult:
            return CredentialPreparationResult(
                credential={**credential, "type": "normalized"},
                changed=True,
            )

    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-old"},
                status=KeyStatus.AVAILABLE,
            )
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(_NormalizingPlugin("openai", ["gpt-4o"], available=True)),
    )

    updated = await service.update_key("key-1", UpdateKeyInput(credential={"api_key": "sk-new"}))

    assert updated.credential == {"api_key": "sk-new", "type": "normalized"}
    assert repository._keys["key-1"].credential == {"api_key": "sk-new", "type": "normalized"}


@pytest.mark.anyio
async def test_update_key_updates_credential_and_restores_admin_disable_with_single_probe() -> None:
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-old"},
                status=KeyStatus.DISABLED_ADMIN,
            )
        ]
    )
    plugin = FakeProviderPlugin(
        "openai",
        ["gpt-4o"],
        available=True,
        capacity_signal=CapacitySignal(
            has_capacity_signal=True,
            capacity_score=0.8,
            quota_available=True,
            capacity_kind="remaining_ratio",
            reason="healthy",
        ),
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )

    updated = await service.update_key(
        "key-1",
        UpdateKeyInput(
            credential={"api_key": "sk-new"},
            status=KeyStatus.AVAILABLE,
        ),
    )

    assert updated.status == KeyStatus.AVAILABLE
    assert updated.credential == {"api_key": "sk-new"}
    assert updated.supported_models == ["gpt-4o"]
    assert plugin.available_checks == [{"api_key": "sk-new"}]
    assert repository._keys["key-1"].status == KeyStatus.AVAILABLE
    assert repository._keys["key-1"].credential == {"api_key": "sk-new"}


@pytest.mark.anyio
async def test_in_memory_allocation_store_skips_removed_keys() -> None:
    store = InMemoryAllocationStore()
    now = datetime.now(timezone.utc)

    await store.remove_key("key-1", "openai")

    allocated = await store.allocate_key("openai", ["key-1", "key-2"], now)

    assert allocated == "key-2"
    assert store.allocate_calls == [("openai", ["key-1", "key-2"])]


@pytest.mark.anyio
async def test_repository_upsert_sets_updated_at() -> None:
    repository = InMemoryKeyRepository()
    key = ApiKey(id="key-1", provider="openai", credential={"api_key": "sk-test"})

    await repository.upsert_key(key)

    assert repository._keys["key-1"].updated_at is not None


@pytest.mark.anyio
async def test_runtime_lock_updates_updated_at() -> None:
    before = datetime.now(timezone.utc) - timedelta(minutes=10)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                last_refreshed_at=before,
                updated_at=before,
            )
        ]
    )
    now = datetime.now(timezone.utc)

    acquired = await repository.acquire_runtime_lock(
        "key-1",
        "owner-1",
        now,
        60,
        "test",
    )

    assert acquired is True


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


@pytest.mark.anyio
async def test_refresh_sets_disabled_upstream_when_credential_unavailable() -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=10)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.AVAILABLE,
                supported_models=["gpt-4o"],
                last_refreshed_at=now,
                cached_available=True,
            )
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(FakeProviderPlugin("openai", ["gpt-4o"], available=False)),
    )

    await service.refresh_keys()

    assert repository._keys["key-1"].status == KeyStatus.DISABLED_UPSTREAM
    assert repository._keys["key-1"].cached_available is False
    assert repository._keys["key-1"].supported_models == []


@pytest.mark.anyio
async def test_refresh_merge_does_not_overwrite_concurrent_usage_updates() -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=10)
    concurrent_last_used = datetime.now(timezone.utc)

    class _ConcurrentUsageRepository(InMemoryKeyRepository):
        async def list_keys_needing_refresh(
            self, cutoff: datetime, provider: str | None = None
        ) -> list[ApiKey]:
            return [deepcopy(key) for key in await super().list_keys_needing_refresh(cutoff, provider)]

        async def update_background_runtime_snapshot_if_locked(
            self,
            key: ApiKey,
            owner: str,
            now: datetime,
        ) -> ApiKey | None:
            self._keys[key.id].success_count = 7
            self._keys[key.id].last_used_at = concurrent_last_used
            return await super().update_background_runtime_snapshot_if_locked(
                key,
                owner,
                now,
            )

    repository = _ConcurrentUsageRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.AVAILABLE,
                last_refreshed_at=now,
                cached_available=True,
            )
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

    await service.refresh_keys()

    assert repository._keys["key-1"].success_count == 7
    assert repository._keys["key-1"].last_used_at == concurrent_last_used
    assert repository._keys["key-1"].cached_available is True


@pytest.mark.anyio
async def test_refresh_does_not_probe_capacity_when_credential_unavailable() -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=10)

    class _NoCapacityProbePlugin(FakeProviderPlugin):
        def __init__(self) -> None:
            super().__init__("openai", ["gpt-4o"], available=False)
            self.capacity_checks = 0

        async def get_capacity_signal(self, credential: dict[str, str]) -> CapacitySignal | None:
            self.capacity_checks += 1
            return await super().get_capacity_signal(credential)

    plugin = _NoCapacityProbePlugin()
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.AVAILABLE,
                last_refreshed_at=now,
            )
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )

    await service.refresh_keys()

    assert plugin.capacity_checks == 0


@pytest.mark.anyio
async def test_refresh_sets_missing_provider_key_unavailable_and_unallocatable() -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=10)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="missing",
                credential={"api_key": "sk-test"},
                status=KeyStatus.AVAILABLE,
                supported_models=["gpt-4o"],
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=0.8,
            )
        ]
    )
    allocation_store = InMemoryAllocationStore()
    service = KeyService(
        repository,
        allocation_store,
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(),
    )

    await service.refresh_keys()

    assert repository._keys["key-1"].status == KeyStatus.DISABLED_UPSTREAM
    assert repository._keys["key-1"].cached_available is False
    assert repository._keys["key-1"].supported_models == []
    assert allocation_store.synced_scores["key-1"] == float("-inf")


@pytest.mark.anyio
async def test_allocate_skips_key_when_provider_plugin_is_missing() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="missing",
                credential={"api_key": "sk-test"},
                status=KeyStatus.AVAILABLE,
                supported_models=["gpt-4o"],
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=0.8,
            )
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(),
    )

    with pytest.raises(NoAvailableKeyError):
        await service.allocate_key("missing", "gpt-4o")


@pytest.mark.anyio
async def test_refresh_does_not_reprepare_registration_credential_before_probing() -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=10)

    class _RefreshCredentialPlugin(FakeProviderPlugin):
        async def prepare_credential(self, credential: dict[str, str]) -> CredentialPreparationResult:
            return CredentialPreparationResult(
                credential={**credential, "access_token": "new-token", "last_refresh": "2026-04-14T00:00:00+00:00"},
                changed=True,
            )

    plugin = _RefreshCredentialPlugin("codex_oauth", ["gpt-5"], available=True)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="codex_oauth",
                credential={"access_token": "old-token"},
                status=KeyStatus.DISABLED_UPSTREAM,
                last_refreshed_at=now,
                cached_available=False,
            )
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )

    await service.refresh_keys()

    assert repository._keys["key-1"].credential["access_token"] == "old-token"
    assert "last_refresh" not in repository._keys["key-1"].credential
    assert plugin.available_checks[0]["access_token"] == "old-token"
    assert repository._keys["key-1"].status == KeyStatus.AVAILABLE


@pytest.mark.anyio
async def test_refresh_keeps_credential_when_prepared_payload_is_equal() -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=10)
    credential = {"access_token": "same-token", "last_refresh": "2026-04-14T00:00:00+00:00"}

    class _SameCredentialPlugin(FakeProviderPlugin):
        async def prepare_credential(self, credential: dict[str, str]) -> CredentialPreparationResult:
            return CredentialPreparationResult(credential=dict(credential), changed=True)

    plugin = _SameCredentialPlugin("codex_oauth", ["gpt-5"], available=True)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="codex_oauth",
                credential=dict(credential),
                last_refreshed_at=now,
                cached_available=True,
            )
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )

    await service.refresh_keys()

    assert repository._keys["key-1"].credential == credential


@pytest.mark.anyio
async def test_refresh_sets_exhausted_when_quota_known_unavailable() -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=10)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openrouter",
                credential={"api_key": "sk-test"},
                status=KeyStatus.AVAILABLE,
                last_refreshed_at=now,
            )
        ]
    )
    plugin = FakeProviderPlugin(
        "openrouter",
        ["gpt-4o"],
        available=True,
        capacity_signal=CapacitySignal(
            has_capacity_signal=True,
            capacity_score=0.0,
            quota_available=False,
            capacity_kind="remaining_budget_ratio",
            reason="exhausted",
        ),
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )

    await service.refresh_keys()

    assert repository._keys["key-1"].status == KeyStatus.EXHAUSTED
    assert repository._keys["key-1"].cached_available is True
    assert repository._keys["key-1"].cached_quota_available is False


@pytest.mark.anyio
async def test_refresh_does_not_override_disabled_admin() -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=10)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.DISABLED_ADMIN,
                last_refreshed_at=now,
            )
        ]
    )
    plugin = FakeProviderPlugin("openai", ["gpt-4o"], available=True)
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )

    await service.refresh_keys()

    assert repository._keys["key-1"].status == KeyStatus.DISABLED_ADMIN


@pytest.mark.anyio
async def test_refresh_failure_does_not_clear_models_for_disabled_admin() -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=10)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.DISABLED_ADMIN,
                supported_models=["gpt-4o"],
                last_refreshed_at=now,
            )
        ]
    )
    plugin = FakeProviderPlugin("openai", ["gpt-4o"], available=False)
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(plugin),
    )

    await service.refresh_keys()

    assert repository._keys["key-1"].status == KeyStatus.DISABLED_ADMIN
    assert repository._keys["key-1"].supported_models == ["gpt-4o"]


@pytest.mark.anyio
async def test_report_success_updates_priority_fields_without_changing_status() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.DISABLED_ADMIN,
                last_used_at=now - timedelta(minutes=10),
            )
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

    updated = await service.report_success("key-1", tokens_used=12)

    assert updated.status == KeyStatus.DISABLED_ADMIN
    assert updated.success_count == 1
    assert updated.quota_used == 12


@pytest.mark.anyio
async def test_report_success_does_not_overwrite_runtime_refresh_fields() -> None:
    now = datetime.now(timezone.utc)

    class _RuntimeWinsRepository(InMemoryKeyRepository):
        async def get_key(self, key_id: str) -> ApiKey | None:
            return deepcopy(await super().get_key(key_id))

        async def record_success(self, key_id: str, tokens_used: int, now: datetime) -> ApiKey | None:
            current = self._keys[key_id]
            current.credential = {"api_key": "new-token"}
            current.cached_available = True
            current.last_refreshed_at = now
            return await super().record_success(key_id, tokens_used, now)

    repository = _RuntimeWinsRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "old-token"},
                status=KeyStatus.AVAILABLE,
            )
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

    updated = await service.report_success("key-1", tokens_used=3)

    assert updated.credential == {"api_key": "new-token"}
    assert repository._keys["key-1"].credential == {"api_key": "new-token"}
    assert updated.success_count == 1


@pytest.mark.anyio
async def test_report_error_updates_priority_fields_without_changing_status() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.EXHAUSTED,
                last_used_at=now - timedelta(minutes=10),
            )
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

    updated = await service.report_error("key-1", "rate_limit")

    assert updated.status == KeyStatus.EXHAUSTED
    assert updated.error_count == 1


@pytest.mark.anyio
async def test_report_error_does_not_overwrite_runtime_refresh_fields() -> None:
    now = datetime.now(timezone.utc)

    class _RuntimeWinsRepository(InMemoryKeyRepository):
        async def get_key(self, key_id: str) -> ApiKey | None:
            return deepcopy(await super().get_key(key_id))

        async def record_error(self, key_id: str, now: datetime) -> ApiKey | None:
            current = self._keys[key_id]
            current.credential = {"api_key": "new-token"}
            current.cached_available = False
            current.status = KeyStatus.DISABLED_UPSTREAM
            current.last_refreshed_at = now
            return await super().record_error(key_id, now)

    repository = _RuntimeWinsRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "old-token"},
                status=KeyStatus.AVAILABLE,
            )
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

    updated = await service.report_error("key-1", "network_timeout")

    assert updated.credential == {"api_key": "new-token"}
    assert updated.status == KeyStatus.DISABLED_UPSTREAM
    assert updated.error_count == 1


@pytest.mark.anyio
async def test_runtime_persist_does_not_override_concurrent_disabled_admin() -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=10)

    class _AdminWinsRepository(InMemoryKeyRepository):
        async def list_keys_needing_refresh(
            self, cutoff: datetime, provider: str | None = None
        ) -> list[ApiKey]:
            return [deepcopy(key) for key in await super().list_keys_needing_refresh(cutoff, provider)]

        async def get_key(self, key_id: str) -> ApiKey | None:
            return deepcopy(await super().get_key(key_id))

        async def acquire_runtime_lock(
            self,
            key_id: str,
            owner: str,
            now: datetime,
            ttl_seconds: int,
            reason: str,
        ) -> bool:
            acquired = await super().acquire_runtime_lock(
                key_id,
                owner,
                now,
                ttl_seconds,
                reason,
            )
            if acquired:
                self._keys[key_id].status = KeyStatus.DISABLED_ADMIN
            return acquired

    repository = _AdminWinsRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.AVAILABLE,
                last_refreshed_at=now,
                cached_available=True,
            )
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

    await service.refresh_keys()

    assert repository._keys["key-1"].status == KeyStatus.DISABLED_ADMIN


@pytest.mark.anyio
async def test_background_runtime_persist_does_not_write_after_concurrent_admin_disable() -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=10)

    class _AdminDisablesDuringPersistRepository(InMemoryKeyRepository):
        async def list_keys_needing_refresh(
            self, cutoff: datetime, provider: str | None = None
        ) -> list[ApiKey]:
            return [deepcopy(key) for key in await super().list_keys_needing_refresh(cutoff, provider)]

        async def get_key(self, key_id: str) -> ApiKey | None:
            return deepcopy(await super().get_key(key_id))

        async def update_background_runtime_snapshot_if_locked(
            self,
            key: ApiKey,
            owner: str,
            now: datetime,
        ) -> ApiKey | None:
            self._keys[key.id].status = KeyStatus.DISABLED_ADMIN
            return await super().update_background_runtime_snapshot_if_locked(key, owner, now)

    repository = _AdminDisablesDuringPersistRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.AVAILABLE,
                supported_models=["old-model"],
                last_refreshed_at=now,
                cached_available=False,
            )
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(FakeProviderPlugin("openai", ["new-model"], available=True)),
    )

    await service.refresh_keys()

    assert repository._keys["key-1"].status == KeyStatus.DISABLED_ADMIN
    assert repository._keys["key-1"].supported_models == ["old-model"]
    assert repository._keys["key-1"].cached_available is False
    assert repository._keys["key-1"].last_refreshed_at == now


@pytest.mark.anyio
async def test_update_key_does_not_overwrite_concurrent_priority_fields() -> None:
    now = datetime.now(timezone.utc)
    concurrent_last_used = now - timedelta(seconds=5)

    class _PriorityWinsRepository(InMemoryKeyRepository):
        async def get_key(self, key_id: str) -> ApiKey | None:
            return deepcopy(await super().get_key(key_id))

        async def update_runtime_snapshot_if_locked(
            self,
            key: ApiKey,
            owner: str,
            now: datetime,
        ) -> ApiKey | None:
            current = self._keys[key.id]
            current.success_count = 9
            current.error_count = 2
            current.last_used_at = concurrent_last_used
            return await super().update_runtime_snapshot_if_locked(key, owner, now)

    class _NormalizingPlugin(FakeProviderPlugin):
        async def prepare_credential(self, credential: dict[str, str]) -> CredentialPreparationResult:
            return CredentialPreparationResult(
                credential={**credential, "type": "normalized"},
                changed=True,
            )

    repository = _PriorityWinsRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-old"},
                status=KeyStatus.AVAILABLE,
                success_count=1,
                error_count=1,
            )
        ]
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(_NormalizingPlugin("openai", ["gpt-4o"], available=True)),
    )

    updated = await service.update_key("key-1", UpdateKeyInput(credential={"api_key": "sk-new"}))

    assert updated.credential == {"api_key": "sk-new", "type": "normalized"}
    assert repository._keys["key-1"].success_count == 9
    assert repository._keys["key-1"].error_count == 2
    assert repository._keys["key-1"].last_used_at == concurrent_last_used


@pytest.mark.anyio
async def test_allocate_key_does_not_resurrect_deleted_key_after_touch_failure() -> None:
    now = datetime.now(timezone.utc)

    class _DeleteOnTouchRepository(InMemoryKeyRepository):
        async def get_key(self, key_id: str) -> ApiKey | None:
            return deepcopy(await super().get_key(key_id))

        async def touch_key_used(self, key_id: str, now: datetime) -> ApiKey | None:
            self._keys.pop(key_id, None)
            return None

    repository = _DeleteOnTouchRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.AVAILABLE,
                supported_models=["gpt-4o"],
                last_refreshed_at=now,
                cached_available=True,
            )
        ]
    )
    allocation_store = InMemoryAllocationStore()
    service = KeyService(
        repository,
        allocation_store,
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(FakeProviderPlugin("openai", ["gpt-4o"], available=True)),
    )

    with pytest.raises(NoAvailableKeyError):
        await service.allocate_key("openai", "gpt-4o")

    assert ("openai", "key-1") in allocation_store.released
    assert "key-1" not in allocation_store.synced_scores
    assert "key-1" not in repository._keys


@pytest.mark.anyio
async def test_report_success_does_not_recreate_deleted_key() -> None:
    now = datetime.now(timezone.utc)

    class _DeleteOnSuccessRepository(InMemoryKeyRepository):
        async def record_success(self, key_id: str, tokens_used: int, now: datetime) -> ApiKey | None:
            self._keys.pop(key_id, None)
            return None

    repository = _DeleteOnSuccessRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.AVAILABLE,
                last_used_at=now - timedelta(minutes=1),
            )
        ]
    )
    allocation_store = InMemoryAllocationStore()
    service = KeyService(
        repository,
        allocation_store,
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(FakeProviderPlugin("openai", ["gpt-4o"], available=True)),
    )

    with pytest.raises(KeyNotFoundError):
        await service.report_success("key-1", tokens_used=10)

    assert "key-1" not in repository._keys
    assert ("openai", "key-1") in allocation_store.released
    assert "key-1" not in allocation_store.synced_scores


@pytest.mark.anyio
async def test_report_error_does_not_recreate_deleted_key() -> None:
    now = datetime.now(timezone.utc)

    class _DeleteOnErrorRepository(InMemoryKeyRepository):
        async def record_error(self, key_id: str, now: datetime) -> ApiKey | None:
            self._keys.pop(key_id, None)
            return None

    repository = _DeleteOnErrorRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.AVAILABLE,
                last_used_at=now - timedelta(minutes=1),
            )
        ]
    )
    allocation_store = InMemoryAllocationStore()
    service = KeyService(
        repository,
        allocation_store,
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        build_provider_registry(FakeProviderPlugin("openai", ["gpt-4o"], available=True)),
    )

    with pytest.raises(KeyNotFoundError):
        await service.report_error("key-1", "network_timeout")

    assert "key-1" not in repository._keys
    assert ("openai", "key-1") in allocation_store.released
    assert "key-1" not in allocation_store.synced_scores


@pytest.mark.anyio
async def test_in_memory_repository_enforces_provider_credential_uniqueness() -> None:
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test", "region": "us"},
            )
        ]
    )

    with pytest.raises(DuplicateCredentialError):
        await repository.upsert_key(
            ApiKey(
                id="key-2",
                provider="openai",
                credential={"region": "us", "api_key": "sk-test"},
            )
        )
