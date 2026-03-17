from datetime import datetime, timezone

import punq
from fastapi.testclient import TestClient

from application.services.key_service import KeyService
from domain.entities.api_key import ApiKey
from domain.services.scheduler import KeyScheduler
from domain.services.scorer import KeyScorer
from domain.services.state_machine import KeyStateMachine
from infrastructure.config.settings import Settings
from interfaces.api.app import create_app
from tests.fakes import FakeProviderPlugin, InMemoryAllocationStore, InMemoryKeyRepository, build_provider_registry


def build_client(plugin_available: bool = True) -> TestClient:
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                api_key="sk-test",
                quota_used=0,
                last_used_at=datetime.now(timezone.utc),
            )
        ]
    )
    allocation_store = InMemoryAllocationStore()
    scorer = KeyScorer()
    scheduler = KeyScheduler(scorer, jitter=0.0)
    state_machine = KeyStateMachine()
    provider_registry = build_provider_registry(
        FakeProviderPlugin("openai", ["gpt-4o", "gpt-4o-mini"], available=plugin_available),
    )
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
    assert payload["api_key"] == "sk-test"

    success_response = client.post(
        "/api/internal/report-success",
        json={"key_id": "key-1", "tokens_used": 12},
        headers={"X-Internal-Key": "test-key"},
    )
    assert success_response.status_code == 200
    assert success_response.json()["key"]["quota_used"] == 12


def test_plugin_unavailable_blocks_allocation() -> None:
    client = build_client(plugin_available=False)

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai"},
        headers={"X-Internal-Key": "test-key"},
    )
    assert response.status_code == 404


def test_internal_auth_rejects_invalid_key() -> None:
    client = build_client()

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai"},
        headers={"X-Internal-Key": "wrong"},
    )
    assert response.status_code == 401


def test_admin_key_crud_and_model_sync() -> None:
    client = build_client()

    create_response = client.post(
        "/api/providers/openai/keys",
        json={"api_key": "sk-new"},
    )
    assert create_response.status_code == 200
    created = create_response.json()
    assert created["provider"] == "openai"
    assert created["supported_models"] == ["gpt-4o", "gpt-4o-mini"]

    key_id = created["id"]

    list_response = client.get("/api/providers/openai/keys")
    assert list_response.status_code == 200
    assert any(item["id"] == key_id for item in list_response.json())

    update_response = client.put(
        f"/api/keys/{key_id}",
        json={"api_key": "sk-updated"},
    )
    assert update_response.status_code == 200
    assert update_response.json()["supported_models"] == ["gpt-4o", "gpt-4o-mini"]

    delete_response = client.delete(f"/api/keys/{key_id}")
    assert delete_response.status_code == 204
