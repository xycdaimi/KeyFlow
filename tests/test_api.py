from datetime import datetime, timedelta, timezone

import pytest
import punq
from fastapi.testclient import TestClient

from application.services.key_service import KeyService
from domain.entities.api_key import ApiKey
from domain.services.scheduler import KeyScheduler
from domain.services.scorer import KeyScorer
from domain.services.state_machine import KeyStateMachine
from domain.value_objects.key_status import KeyStatus
from infrastructure.config.settings import Settings
from infrastructure.plugins.base import CapacitySignal, ProviderRegistry
from interfaces.api import app as api_app_module
from interfaces.api.app import create_app, ensure_schema_ready
from tests.fakes import FakeProviderPlugin, InMemoryAllocationStore, InMemoryKeyRepository, build_provider_registry


def build_client(plugin_available: bool = True) -> TestClient:
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                quota_used=0,
                last_used_at=datetime.now(timezone.utc),
            )
        ]
    )
    allocation_store = InMemoryAllocationStore()
    scorer = KeyScorer()
    scheduler = KeyScheduler(scorer, jitter=0.0)
    state_machine = KeyStateMachine()
    plugin = FakeProviderPlugin("openai", ["gpt-4o", "gpt-4o-mini"], available=plugin_available)
    provider_registry = build_provider_registry(plugin)
    service = KeyService(repository, allocation_store, scheduler, scorer, state_machine, provider_registry)

    container = punq.Container()
    settings = Settings(
        APP_NAME="KeyFlowTest",
        API_PREFIX="/api",
        INTERNAL_API_KEY="test-key",
        DATABASE_URL_READ="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        DATABASE_URL_WRITE="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        REDIS_URL="redis://localhost:6379/9",
    )
    container.register(KeyService, instance=service)
    container.register(Settings, instance=settings)
    container.register(ProviderRegistry, instance=provider_registry)
    app = create_app(container=container, settings=settings)
    app.state.test_plugin = plugin
    app.state.test_allocation_store = allocation_store
    return TestClient(app)


def test_allocate_and_report_cycle() -> None:
    client = build_client()

    allocate_response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai"},
        headers={"X-Internal-Key": "test-key"},
    )
    assert allocate_response.status_code == 200
    payload = allocate_response.json()
    assert payload["status"] == "ok"
    assert payload["key_id"] == "key-1"
    assert payload["credential"] == {"api_key": "sk-test"}

    success_response = client.post(
        "/api/internal/report-success",
        json={"key_id": "key-1", "tokens_used": 12},
        headers={"X-Internal-Key": "test-key"},
    )
    assert success_response.status_code == 200
    assert success_response.json()["key"]["quota_used"] == 12

    allocation_store: InMemoryAllocationStore = client.app.state.test_allocation_store
    assert ("openai", "key-1") in allocation_store.released


def test_allocate_recovers_expired_cooldown_inline() -> None:
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-cooldown",
                provider="openai",
                credential={"api_key": "sk-cooldown"},
                status=KeyStatus.RATE_LIMITED,
                cooldown_until=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        ]
    )
    allocation_store = InMemoryAllocationStore()
    scorer = KeyScorer()
    scheduler = KeyScheduler(scorer, jitter=0.0)
    state_machine = KeyStateMachine()
    plugin = FakeProviderPlugin("openai", ["gpt-4o"], available=True)
    provider_registry = build_provider_registry(plugin)
    service = KeyService(repository, allocation_store, scheduler, scorer, state_machine, provider_registry)

    container = punq.Container()
    settings = Settings(
        APP_NAME="KeyFlowTest",
        API_PREFIX="/api",
        INTERNAL_API_KEY="test-key",
        DATABASE_URL_READ="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        DATABASE_URL_WRITE="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        REDIS_URL="redis://localhost:6379/9",
    )
    container.register(KeyService, instance=service)
    container.register(Settings, instance=settings)
    app = create_app(container=container, settings=settings)
    client = TestClient(app)

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai"},
        headers={"X-Internal-Key": "test-key"},
    )
    assert response.status_code == 200
    assert response.json()["key_id"] == "key-cooldown"
    assert repository._keys["key-cooldown"].status == KeyStatus.AVAILABLE
    assert repository._keys["key-cooldown"].cooldown_until is None


def test_report_error_accepts_generic_error_type() -> None:
    client = build_client()

    response = client.post(
        "/api/internal/report-error",
        json={"key_id": "key-1", "error_type": "network_timeout"},
        headers={"X-Internal-Key": "test-key"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["key"]["status"] == "available"
    allocation_store: InMemoryAllocationStore = client.app.state.test_allocation_store
    assert ("openai", "key-1") in allocation_store.released


def test_plugin_unavailable_blocks_allocation() -> None:
    client = build_client(plugin_available=False)

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai"},
        headers={"X-Internal-Key": "test-key"},
    )
    assert response.status_code == 404


def test_allocate_passes_model_to_plugin() -> None:
    client = build_client()

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai", "model": "gpt-4o"},
        headers={"X-Internal-Key": "test-key"},
    )
    assert response.status_code == 200

    plugin: FakeProviderPlugin = client.app.state.test_plugin
    assert plugin.available_checks
    assert plugin.available_checks[-1][1] == "gpt-4o"
    assert plugin.available_checks[-1][0] == {"api_key": "sk-test"}


def test_allocate_uses_supported_models_local_prefilter() -> None:
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-no-match",
                provider="openai",
                credential={"api_key": "sk-bad"},
                supported_models=["gpt-3.5-turbo"],
            ),
            ApiKey(
                id="key-match",
                provider="openai",
                credential={"api_key": "sk-good"},
                supported_models=["gpt-4o"],
            ),
        ]
    )
    allocation_store = InMemoryAllocationStore()
    scorer = KeyScorer()
    scheduler = KeyScheduler(scorer, jitter=0.0)
    state_machine = KeyStateMachine()
    plugin = FakeProviderPlugin("openai", ["gpt-4o"], available=True)
    provider_registry = build_provider_registry(plugin)
    service = KeyService(repository, allocation_store, scheduler, scorer, state_machine, provider_registry)

    container = punq.Container()
    settings = Settings(
        APP_NAME="KeyFlowTest",
        API_PREFIX="/api",
        INTERNAL_API_KEY="test-key",
        DATABASE_URL_READ="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        DATABASE_URL_WRITE="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        REDIS_URL="redis://localhost:6379/9",
    )
    container.register(KeyService, instance=service)
    container.register(Settings, instance=settings)
    app = create_app(container=container, settings=settings)
    client = TestClient(app)

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai", "model": "gpt-4o"},
        headers={"X-Internal-Key": "test-key"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["key_id"] == "key-match"


def test_allocate_prefers_higher_capacity_signal_when_health_equal() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-low",
                provider="openrouter",
                credential={"api_key": "sk-low"},
                last_used_at=now - timedelta(minutes=5),
            ),
            ApiKey(
                id="key-high",
                provider="openrouter",
                credential={"api_key": "sk-high"},
                last_used_at=now - timedelta(minutes=5),
            ),
        ]
    )
    allocation_store = InMemoryAllocationStore()
    scorer = KeyScorer()
    scheduler = KeyScheduler(scorer, jitter=0.0)
    state_machine = KeyStateMachine()
    plugin = FakeProviderPlugin(
        "openrouter",
        ["gpt-4o"],
        available=True,
        capacity_by_credential={
            (("api_key", "sk-low"),): CapacitySignal(
                has_capacity_signal=True,
                capacity_score=0.1,
                capacity_kind="remaining_budget_ratio",
                reason="low remaining budget",
            ),
            (("api_key", "sk-high"),): CapacitySignal(
                has_capacity_signal=True,
                capacity_score=0.9,
                capacity_kind="remaining_budget_ratio",
                reason="high remaining budget",
            ),
        },
    )
    provider_registry = build_provider_registry(plugin)
    service = KeyService(repository, allocation_store, scheduler, scorer, state_machine, provider_registry)

    container = punq.Container()
    settings = Settings(
        APP_NAME="KeyFlowTest",
        API_PREFIX="/api",
        INTERNAL_API_KEY="test-key",
        DATABASE_URL_READ="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        DATABASE_URL_WRITE="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        REDIS_URL="redis://localhost:6379/9",
    )
    container.register(KeyService, instance=service)
    container.register(Settings, instance=settings)
    container.register(ProviderRegistry, instance=provider_registry)
    app = create_app(container=container, settings=settings)
    client = TestClient(app)

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openrouter"},
        headers={"X-Internal-Key": "test-key"},
    )

    assert response.status_code == 200
    assert response.json()["key_id"] == "key-high"


def test_internal_auth_rejects_invalid_key() -> None:
    client = build_client()

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai"},
        headers={"X-Internal-Key": "wrong"},
    )
    assert response.status_code == 401


def test_default_create_app_initializes_container() -> None:
    app = create_app()
    service = app.state.container.resolve(KeyService)
    assert isinstance(service, KeyService)
    assert app.description == "Provider-scoped API key scheduling service."
    assert app.version == "0.1.0"


@pytest.mark.anyio
async def test_ensure_schema_ready_retries_transient_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    create_all_calls = 0

    class _FakeConnection:
        async def run_sync(self, fn) -> None:
            nonlocal create_all_calls
            create_all_calls += 1

    class _FakeBegin:
        async def __aenter__(self):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError("transient db startup failure")
            return _FakeConnection()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeEngine:
        def begin(self) -> _FakeBegin:
            return _FakeBegin()

    async def _fake_sleep(_: int) -> None:
        return None

    monkeypatch.setattr(api_app_module, "DB_SCHEMA_INIT_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(api_app_module, "DB_SCHEMA_INIT_RETRY_SECONDS", 0)
    monkeypatch.setattr(api_app_module.asyncio, "sleep", _fake_sleep)

    await ensure_schema_ready(_FakeEngine())

    assert attempts == 3
    assert create_all_calls == 1


@pytest.mark.anyio
async def test_ensure_schema_ready_raises_after_retry_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    class _FakeBegin:
        async def __aenter__(self):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("db unavailable")

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeEngine:
        def begin(self) -> _FakeBegin:
            return _FakeBegin()

    async def _fake_sleep(_: int) -> None:
        return None

    monkeypatch.setattr(api_app_module, "DB_SCHEMA_INIT_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(api_app_module, "DB_SCHEMA_INIT_RETRY_SECONDS", 0)
    monkeypatch.setattr(api_app_module.asyncio, "sleep", _fake_sleep)

    with pytest.raises(RuntimeError, match="db unavailable"):
        await ensure_schema_ready(_FakeEngine())

    assert attempts == 3


def test_admin_key_crud_and_model_sync() -> None:
    client = build_client()

    create_response = client.post(
        "/api/providers/openai/keys",
        json={"credential": {"api_key": "sk-new"}},
    )
    assert create_response.status_code == 200
    assert create_response.json() == {"status": "ok"}

    list_response = client.get("/api/providers/openai/keys")
    assert list_response.status_code == 200
    items = list_response.json()
    assert len(items) == 2
    created = next(item for item in items if item["credential"] == {"api_key": "sk-new"})
    assert created == {
        "key_id": created["key_id"],
        "credential": {"api_key": "sk-new"},
        "status": "available",
    }

    key_id = created["key_id"]

    update_response = client.put(
        f"/api/keys/{key_id}",
        json={"credential": {"api_key": "sk-updated"}},
    )
    assert update_response.status_code == 200
    assert update_response.json() == {"status": "ok"}

    get_key_response = client.get(f"/api/keys/{key_id}")
    assert get_key_response.status_code == 200
    assert get_key_response.json() == {
        "credential": {"api_key": "sk-updated"},
        "status": "available",
    }

    get_models_response = client.get(f"/api/providers/openai/keys/{key_id}/models")
    assert get_models_response.status_code == 200
    assert get_models_response.json() == {"models": ["gpt-4o", "gpt-4o-mini"]}

    delete_response = client.delete(f"/api/keys/{key_id}")
    assert delete_response.status_code == 200
    assert delete_response.json() == {"status": "ok"}


def test_get_key_models_rejects_provider_mismatch() -> None:
    client = build_client()

    response = client.get("/api/providers/anthropic/keys/key-1/models")

    assert response.status_code == 404
    assert response.json() == {"detail": "key_not_found"}


def test_list_providers_returns_plugin_metadata() -> None:
    client = build_client()

    response = client.get("/api/providers")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["name"] == "openai"
    assert payload[0]["auth_type"] == "bearer_api_key"
    assert payload[0]["model_source"] == "remote"
    assert payload[0]["available"] is True


def test_explain_key_returns_safe_plugin_summary() -> None:
    client = build_client()

    response = client.get("/api/keys/key-1/explain")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "openai"
    assert payload["available"] is True
    assert "sk-test" not in str(payload)
