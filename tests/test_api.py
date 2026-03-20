import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import punq
from fastapi.testclient import TestClient

from application.services.key_service import KeyService
from domain.entities.api_key import ApiKey
from domain.exceptions.domain_exceptions import NoAvailableKeyError
from domain.services.scheduler import KeyScheduler
from domain.services.scorer import KeyScorer
from domain.services.state_machine import KeyStateMachine
from domain.value_objects.key_status import KeyStatus
from infrastructure.config.settings import Settings
from infrastructure.db.repository_impl import SqlAlchemyKeyRepository
from infrastructure.plugins.base import CapacitySignal, ProviderRegistry
from interfaces.api import app as api_app_module
from interfaces.api.app import create_app, ensure_schema_ready
from tests.fakes import FakeProviderPlugin, InMemoryAllocationStore, InMemoryKeyRepository, build_provider_registry


def build_settings() -> Settings:
    return Settings(
        APP_NAME="KeyFlowTest",
        API_PREFIX="/api",
        INTERNAL_API_KEY="test-key",
        DATABASE_URL_READ="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        DATABASE_URL_WRITE="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        REDIS_URL="redis://localhost:6379/9",
    )


HealthCheckFn = Callable[[], Awaitable[tuple[bool, str | None]]]
ADMIN_HEADERS = {"X-Internal-Key": "test-key"}


def build_test_client(
    *,
    repository: InMemoryKeyRepository,
    plugins: list[FakeProviderPlugin],
    allocation_store: InMemoryAllocationStore | None = None,
    health_checkers: dict[str, HealthCheckFn] | None = None,
) -> TestClient:
    resolved_allocation_store = allocation_store or InMemoryAllocationStore()
    scorer = KeyScorer()
    scheduler = KeyScheduler(scorer, jitter=0.0)
    state_machine = KeyStateMachine()
    provider_registry = build_provider_registry(*plugins)
    service = KeyService(repository, resolved_allocation_store, scheduler, scorer, state_machine, provider_registry)

    container = punq.Container()
    settings = build_settings()
    container.register(KeyService, instance=service)
    container.register(Settings, instance=settings)
    container.register(ProviderRegistry, instance=provider_registry)
    app = create_app(container=container, settings=settings)
    app.state.test_allocation_store = resolved_allocation_store
    app.state.test_plugins = {plugin.name: plugin for plugin in plugins}
    if len(plugins) == 1:
        app.state.test_plugin = plugins[0]
    if health_checkers is not None:
        app.state.health_checkers = health_checkers
    return TestClient(app)


def build_client(plugin_available: bool = True) -> TestClient:
    now = datetime.now(timezone.utc)
    plugin = FakeProviderPlugin("openai", ["gpt-4o", "gpt-4o-mini"], available=plugin_available)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                quota_used=0,
                last_used_at=now,
                last_refreshed_at=now,
                cached_available=plugin_available,
                cached_capacity_score=None,
            )
        ]
    )
    return build_test_client(repository=repository, plugins=[plugin])


def build_cross_provider_client(
    keys: list[ApiKey] | None = None,
    plugins: list[FakeProviderPlugin] | None = None,
) -> TestClient:
    resolved_plugins = (
        [
            FakeProviderPlugin("openai", ["gpt-4o", "gpt-4o-mini"], available=True),
            FakeProviderPlugin("openrouter", ["gpt-4o"], available=True),
        ]
        if plugins is None
        else plugins
    )
    now = datetime.now(timezone.utc)
    resolved_keys = (
        [
            ApiKey(
                id="key-openai",
                provider="openai",
                credential={"api_key": "sk-openai"},
                supported_models=["gpt-4o", "gpt-4o-mini"],
                last_used_at=now,
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=None,
            ),
            ApiKey(
                id="key-openrouter",
                provider="openrouter",
                credential={"api_key": "sk-openrouter"},
                supported_models=["gpt-4o"],
                last_used_at=now,
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=None,
            ),
        ]
        if keys is None
        else keys
    )
    repository = InMemoryKeyRepository(resolved_keys)
    return build_test_client(repository=repository, plugins=resolved_plugins)


def test_health_reports_ready_when_dependencies_ok() -> None:
    async def check_app() -> tuple[bool, str | None]:
        return True, None

    async def check_database() -> tuple[bool, str | None]:
        return True, None

    async def check_redis() -> tuple[bool, str | None]:
        return True, None

    repository = InMemoryKeyRepository([])
    plugin = FakeProviderPlugin("openai", ["gpt-4o"], available=True)
    client = build_test_client(
        repository=repository,
        plugins=[plugin],
        health_checkers={
            "app": check_app,
            "database": check_database,
            "redis": check_redis,
        },
    )

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {
            "app": {"status": "ok", "detail": None},
            "database": {"status": "ok", "detail": None},
            "redis": {"status": "ok", "detail": None},
        },
    }


def test_health_returns_503_degraded_when_dependency_fails() -> None:
    async def check_app() -> tuple[bool, str | None]:
        return True, None

    async def check_database() -> tuple[bool, str | None]:
        return False, "connection refused"

    async def check_redis() -> tuple[bool, str | None]:
        return True, None

    repository = InMemoryKeyRepository([])
    plugin = FakeProviderPlugin("openai", ["gpt-4o"], available=True)
    client = build_test_client(
        repository=repository,
        plugins=[plugin],
        health_checkers={
            "app": check_app,
            "database": check_database,
            "redis": check_redis,
        },
    )

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "checks": {
            "app": {"status": "ok", "detail": None},
            "database": {"status": "error", "detail": "connection refused"},
            "redis": {"status": "ok", "detail": None},
        },
    }


def test_cross_provider_helper_preserves_explicit_empty_plugins() -> None:
    client = build_cross_provider_client(plugins=[])

    response = client.get("/api/providers", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    assert response.json() == []


def test_cross_provider_helper_preserves_explicit_empty_keys() -> None:
    client = build_cross_provider_client(
        keys=[],
        plugins=[FakeProviderPlugin("openai", ["gpt-4o"], available=True)],
    )

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai"},
        headers={"X-Internal-Key": "test-key"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "no_available_key"}


def test_allocate_and_report_cycle() -> None:
    client = build_client()

    allocate_response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai"},
        headers={"X-Internal-Key": "test-key"},
    )
    assert allocate_response.status_code == 200
    payload = allocate_response.json()
    assert payload["key_id"] == "key-1"
    assert payload["credential"] == {"api_key": "sk-test"}

    success_response = client.post(
        "/api/internal/report-success",
        json={"key_id": "key-1", "tokens_used": 12},
        headers={"X-Internal-Key": "test-key"},
    )
    assert success_response.status_code == 200
    assert success_response.json()["quota_used"] == 12

    allocation_store: InMemoryAllocationStore = client.app.state.test_allocation_store
    assert ("openai", "key-1") in allocation_store.released


def test_allocate_recovers_expired_cooldown_inline() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-cooldown",
                provider="openai",
                credential={"api_key": "sk-cooldown"},
                status=KeyStatus.RATE_LIMITED,
                cooldown_until=now - timedelta(seconds=1),
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=None,
            )
        ]
    )
    plugin = FakeProviderPlugin("openai", ["gpt-4o"], available=True)
    client = build_test_client(repository=repository, plugins=[plugin])

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
    assert payload["status"] == "available"
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
    """Allocation with model uses cached availability; model filters by supported_models."""
    client = build_client()

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai", "model": "gpt-4o"},
        headers={"X-Internal-Key": "test-key"},
    )
    assert response.status_code == 200
    assert response.json()["key_id"] == "key-1"


def test_allocate_uses_supported_models_local_prefilter() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-no-match",
                provider="openai",
                credential={"api_key": "sk-bad"},
                supported_models=["gpt-3.5-turbo"],
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=None,
            ),
            ApiKey(
                id="key-match",
                provider="openai",
                credential={"api_key": "sk-good"},
                supported_models=["gpt-4o"],
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=None,
            ),
        ]
    )
    plugin = FakeProviderPlugin("openai", ["gpt-4o"], available=True)
    client = build_test_client(repository=repository, plugins=[plugin])

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai", "model": "gpt-4o"},
        headers={"X-Internal-Key": "test-key"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["key_id"] == "key-match"


def test_allocate_key_route_still_requires_provider() -> None:
    client = build_client()

    response = client.post(
        "/api/internal/allocate-key",
        json={"model": "gpt-4o"},
        headers={"X-Internal-Key": "test-key"},
    )

    assert response.status_code == 422


def test_allocate_by_model_returns_provider_and_credential() -> None:
    client = build_client()

    response = client.post(
        "/api/internal/allocate-by-model",
        json={"model": "gpt-4o"},
        headers={"X-Internal-Key": "test-key"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "key_id": "key-1",
        "provider": "openai",
        "credential": {"api_key": "sk-test"},
    }


def test_allocate_key_remains_provider_scoped_with_cross_provider_route_present() -> None:
    now = datetime.now(timezone.utc)
    client = build_cross_provider_client(
        keys=[
            ApiKey(
                id="key-openai",
                provider="openai",
                credential={"api_key": "sk-openai"},
                supported_models=["gpt-4o"],
                last_used_at=now - timedelta(minutes=5),
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=0.1,
            ),
            ApiKey(
                id="key-openrouter",
                provider="openrouter",
                credential={"api_key": "sk-openrouter"},
                supported_models=["gpt-4o"],
                last_used_at=now - timedelta(minutes=5),
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=0.9,
            ),
        ],
        plugins=[
            FakeProviderPlugin(
                "openai",
                ["gpt-4o"],
                available=True,
                capacity_signal=CapacitySignal(
                    has_capacity_signal=True,
                    capacity_score=0.1,
                    capacity_kind="remaining_budget_ratio",
                    reason="lower capacity but correct provider",
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
                    reason="higher capacity but other provider",
                ),
            ),
        ],
    )

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai", "model": "gpt-4o"},
        headers={"X-Internal-Key": "test-key"},
    )

    assert response.status_code == 200
    assert response.json()["key_id"] == "key-openai"


@pytest.mark.anyio
async def test_allocate_by_model_passes_ranked_candidates_to_allocation_store() -> None:
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
    allocation_store = InMemoryAllocationStore()
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
    assert allocation_store.any_provider_calls == [
        {
            "ordered_candidates": [
                ("openrouter", "openrouter-high"),
                ("openai", "openai-low"),
            ],
            "lease_seconds": 2,
        }
    ]


def test_allocate_by_model_returns_404_when_no_key_available(monkeypatch: pytest.MonkeyPatch) -> None:
    client = build_client()
    service: KeyService = client.app.state.container.resolve(KeyService)

    async def _fake_allocate_key_by_model(model: str) -> ApiKey:
        raise NoAvailableKeyError(f"no available key for {model}")

    monkeypatch.setattr(service, "allocate_key_by_model", _fake_allocate_key_by_model, raising=False)

    response = client.post(
        "/api/internal/allocate-by-model",
        json={"model": "gpt-4o"},
        headers={"X-Internal-Key": "test-key"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "no_available_key"}


def test_allocate_by_model_returns_404_when_model_not_supported_anywhere() -> None:
    client = build_cross_provider_client()

    response = client.post(
        "/api/internal/allocate-by-model",
        json={"model": "claude-3-opus"},
        headers={"X-Internal-Key": "test-key"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "no_available_key"}


def test_allocate_prefers_higher_capacity_signal_when_health_equal() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-low",
                provider="openrouter",
                credential={"api_key": "sk-low"},
                last_used_at=now - timedelta(minutes=5),
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=0.1,
            ),
            ApiKey(
                id="key-high",
                provider="openrouter",
                credential={"api_key": "sk-high"},
                last_used_at=now - timedelta(minutes=5),
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=0.9,
            ),
        ]
    )
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
    client = build_test_client(repository=repository, plugins=[plugin])

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


@pytest.mark.anyio
async def test_lifespan_skips_runtime_startup_when_dependencies_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="interfaces.api.app")

    class _MissingContainer:
        def resolve(self, dependency):
            raise punq.MissingDependencyError(f"missing {dependency}")

    app = SimpleNamespace(state=SimpleNamespace(container=_MissingContainer()))

    async with api_app_module.lifespan(app):
        pass

    assert "event=lifespan_runtime_dependencies_missing" in caplog.text


@pytest.mark.anyio
async def test_lifespan_reraises_unexpected_resolution_errors() -> None:
    class _BoomContainer:
        def resolve(self, dependency):
            if dependency is SqlAlchemyKeyRepository:
                raise RuntimeError("unexpected resolution failure")
            raise AssertionError("unexpected dependency lookup")

    app = SimpleNamespace(state=SimpleNamespace(container=_BoomContainer()))

    with pytest.raises(RuntimeError, match="unexpected resolution failure"):
        async with api_app_module.lifespan(app):
            pass


@pytest.mark.anyio
async def test_api_lifespan_does_not_start_background_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    async def _fake_ensure_schema_ready(_engine) -> None:
        return None

    async def _fake_ensure_refresh_columns(_conn) -> None:
        return None

    class _FakeTask:
        def cancel(self) -> None:
            return None

        def __await__(self):
            async def _noop() -> None:
                return None

            return _noop().__await__()

    def _fake_create_task(coro):
        nonlocal called
        called = True
        if inspect.iscoroutine(coro):
            coro.close()
        return _FakeTask()

    class _FakeConnection:
        pass

    class _FakeBegin:
        async def __aenter__(self) -> _FakeConnection:
            return _FakeConnection()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class _FakeEngine:
        def begin(self) -> _FakeBegin:
            return _FakeBegin()

        async def dispose(self) -> None:
            return None

    class _FakeFactory:
        def __init__(self, engine: _FakeEngine) -> None:
            self.kw = {"bind": engine}

    class _FakeRepository:
        def __init__(self) -> None:
            engine = _FakeEngine()
            self._write_factory = _FakeFactory(engine)
            self._read_factory = _FakeFactory(engine)

    class _FakeRedis:
        async def aclose(self) -> None:
            return None

    class _FakeRedisCache:
        def __init__(self) -> None:
            self._redis = _FakeRedis()

    class _FakeKeyService:
        pass

    class _FakeSettings:
        background_task_interval_seconds = 23

    class _Container:
        def __init__(self) -> None:
            self._repository = _FakeRepository()
            self._cache = _FakeRedisCache()
            self._service = _FakeKeyService()
            self._settings = _FakeSettings()

        def resolve(self, dependency):
            if dependency is SqlAlchemyKeyRepository:
                return self._repository
            if dependency.__name__ == "RedisKeyCache":
                return self._cache
            if dependency is KeyService:
                return self._service
            if dependency is Settings:
                return self._settings
            raise AssertionError(f"unexpected dependency lookup: {dependency}")

    monkeypatch.setattr(api_app_module, "ensure_schema_ready", _fake_ensure_schema_ready)
    monkeypatch.setattr(api_app_module, "ensure_refresh_columns", _fake_ensure_refresh_columns)
    monkeypatch.setattr(api_app_module.asyncio, "create_task", _fake_create_task)

    app = SimpleNamespace(state=SimpleNamespace(container=_Container()))

    async with api_app_module.lifespan(app):
        pass

    assert called is False


@pytest.mark.anyio
async def test_background_phase_failure_logs_phase_continues_loop_and_other_phase(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One phase failing logs phase name; same iteration still runs the other phase; loop continues."""
    from interfaces.workers.background import run_worker_loop

    caplog.set_level(logging.WARNING, logger="interfaces.workers.background")

    class _BgKeyService:
        def __init__(self) -> None:
            self.recover_calls = 0
            self.refresh_calls = 0

        async def recover_cooldowns(self) -> int:
            self.recover_calls += 1
            raise RuntimeError("recover failed")

        async def refresh_keys(self) -> int:
            self.refresh_calls += 1
            return 1

    svc = _BgKeyService()
    stop = asyncio.Event()
    bg = asyncio.create_task(run_worker_loop(svc, interval_seconds=0, stop_event=stop))
    await asyncio.sleep(0.05)
    stop.set()
    await bg

    assert svc.refresh_calls >= 1, "refresh_keys must run even when recover_cooldowns raises"
    assert svc.recover_calls >= 2, "loop must continue for later iterations after a phase failure"
    messages = " ".join(r.getMessage() for r in caplog.records if r.name == "interfaces.workers.background")
    assert "recover_cooldowns" in messages or "phase=recover_cooldowns" in messages


def test_admin_key_crud_and_model_sync() -> None:
    client = build_client()

    create_response = client.post(
        "/api/providers/openai/keys",
        json={"credential": {"api_key": "sk-new"}},
        headers=ADMIN_HEADERS,
    )
    assert create_response.status_code == 200
    create_payload = create_response.json()
    assert create_payload["status"] == "ok"
    assert "key_id" in create_payload
    key_id = create_payload["key_id"]

    list_response = client.get("/api/providers/openai/keys", headers=ADMIN_HEADERS)
    assert list_response.status_code == 200
    items = list_response.json()
    assert len(items) == 2
    created = next(item for item in items if item["credential"] == {"api_key": "sk-new"})
    assert created["key_id"] == key_id
    assert created == {
        "key_id": key_id,
        "credential": {"api_key": "sk-new"},
        "status": "available",
    }

    update_response = client.put(
        f"/api/keys/{key_id}",
        json={"credential": {"api_key": "sk-updated"}},
        headers=ADMIN_HEADERS,
    )
    assert update_response.status_code == 200
    assert update_response.json() == {"status": "ok"}

    get_key_response = client.get(f"/api/keys/{key_id}", headers=ADMIN_HEADERS)
    assert get_key_response.status_code == 200
    assert get_key_response.json() == {
        "credential": {"api_key": "sk-updated"},
        "status": "available",
    }

    get_models_response = client.get(f"/api/providers/openai/keys/{key_id}/models", headers=ADMIN_HEADERS)
    assert get_models_response.status_code == 200
    assert get_models_response.json() == {"models": ["gpt-4o", "gpt-4o-mini"]}

    delete_response = client.delete(f"/api/keys/{key_id}", headers=ADMIN_HEADERS)
    assert delete_response.status_code == 200
    assert delete_response.json() == {"status": "ok"}


def test_admin_routes_require_internal_key() -> None:
    client = build_client()

    response = client.get("/api/providers")

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid internal key"}


def test_create_key_rejects_duplicate_credential_within_same_provider() -> None:
    client = build_client()

    response = client.post(
        "/api/providers/openai/keys",
        json={"credential": {"api_key": "sk-test"}},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "duplicate_credential"}


def test_create_key_rejects_unknown_provider() -> None:
    client = build_client()

    response = client.post(
        "/api/providers/missing/keys",
        json={"credential": {"api_key": "sk-test"}},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "provider_not_found"}


def test_create_key_rejects_provider_that_is_not_ready() -> None:
    repository = InMemoryKeyRepository()
    plugin = FakeProviderPlugin("gemini-web-proxy", ["gemini-2.5-pro"], available=True, plugin_ready=False)
    client = build_test_client(repository=repository, plugins=[plugin])

    response = client.post(
        "/api/providers/gemini-web-proxy/keys",
        json={"credential": {"secure_1psid": "a", "secure_1psidts": "b"}},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "provider_not_ready"}


def test_update_key_rejects_duplicate_credential_within_same_provider() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-a",
                provider="openai",
                credential={"api_key": "sk-a"},
                last_refreshed_at=now,
                cached_available=True,
            ),
            ApiKey(
                id="key-b",
                provider="openai",
                credential={"api_key": "sk-b"},
                last_refreshed_at=now,
                cached_available=True,
            ),
        ]
    )
    plugin = FakeProviderPlugin("openai", ["gpt-4o"], available=True)
    client = build_test_client(repository=repository, plugins=[plugin])

    response = client.put(
        "/api/keys/key-b",
        json={"credential": {"api_key": "sk-a"}},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "duplicate_credential"}


def test_get_key_models_rejects_provider_mismatch() -> None:
    client = build_client()

    response = client.get("/api/providers/anthropic/keys/key-1/models", headers=ADMIN_HEADERS)

    assert response.status_code == 404
    assert response.json() == {"detail": "key_not_found"}


def test_list_providers_returns_multiple_plugins_with_cross_provider_fixture() -> None:
    client = build_cross_provider_client()

    response = client.get("/api/providers", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert {item["name"] for item in payload} == {"openai", "openrouter"}
    assert len(payload) == 2


def test_list_providers_returns_plugin_metadata() -> None:
    client = build_client()

    response = client.get("/api/providers", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]["name"] == "openai"
    assert payload[0]["auth_type"] == "bearer_api_key"
    assert payload[0]["model_source"] == "remote"
    assert payload[0]["available"] is True


def test_explain_key_returns_safe_plugin_summary() -> None:
    client = build_client()

    response = client.get("/api/keys/key-1/explain", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "openai"
    assert payload["available"] is True
    assert "sk-test" not in str(payload)
