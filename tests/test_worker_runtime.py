"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-07
@Description: Sidecar worker runtime 契约测试
"""
from __future__ import annotations

import importlib
import logging
from datetime import datetime, timedelta, timezone

import pytest

from application.services.key_service import KeyService
from domain.entities.api_key import ApiKey
from domain.services.scheduler import KeyScheduler
from domain.services.scorer import KeyScorer
from domain.services.state_machine import KeyStateMachine
from domain.value_objects.key_status import KeyStatus
from tests.fakes import FakeProviderPlugin, InMemoryAllocationStore, InMemoryKeyRepository, build_provider_registry


def build_key_service(keys: list[ApiKey], plugin: FakeProviderPlugin) -> tuple[KeyService, InMemoryKeyRepository]:
    repository = InMemoryKeyRepository(keys)
    allocation_store = InMemoryAllocationStore()
    scorer = KeyScorer()
    scheduler = KeyScheduler(scorer, jitter=0.0)
    state_machine = KeyStateMachine()
    provider_registry = build_provider_registry(plugin)
    service = KeyService(repository, allocation_store, scheduler, scorer, state_machine, provider_registry)
    return service, repository


@pytest.mark.anyio
async def test_worker_runtime_runs_recover_then_refresh_once() -> None:
    background_module = importlib.import_module("interfaces.workers.background")
    calls: list[str] = []

    class _FakeKeyService:
        async def recover_cooldowns(self) -> int:
            calls.append("recover")
            return 1

        async def refresh_keys(self) -> int:
            calls.append("refresh")
            return 1

    recovered, refreshed = await background_module.run_worker_iteration(_FakeKeyService())

    assert (recovered, refreshed) == (1, 1)
    assert calls == ["recover", "refresh"]


@pytest.mark.anyio
async def test_worker_runtime_logs_phase_and_continues_when_recover_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    background_module = importlib.import_module("interfaces.workers.background")
    calls: list[str] = []
    caplog.set_level(logging.WARNING, logger="interfaces.workers.background")

    class _FakeKeyService:
        async def recover_cooldowns(self) -> int:
            calls.append("recover")
            raise RuntimeError("recover failed")

        async def refresh_keys(self) -> int:
            calls.append("refresh")
            return 1

    recovered, refreshed = await background_module.run_worker_iteration(_FakeKeyService())

    assert (recovered, refreshed) == (0, 1)
    assert calls == ["recover", "refresh"]
    assert "phase=recover_cooldowns" in caplog.text


def test_worker_main_uses_settings_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    worker_main_module = importlib.import_module("worker_main")
    observed: dict[str, int] = {}

    class _FakeSettings:
        background_task_interval_seconds = 23

    class _FakeService:
        pass

    async def _fake_run_worker_loop(service, interval_seconds, stop_event):
        observed["interval_seconds"] = interval_seconds
        observed["service_is_fake"] = int(isinstance(service, _FakeService))
        observed["stop_event_created"] = int(stop_event is not None)

    monkeypatch.setattr(worker_main_module, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(worker_main_module, "build_worker_key_service", lambda settings: _FakeService())
    monkeypatch.setattr(worker_main_module, "run_worker_loop", _fake_run_worker_loop)

    worker_main_module.main()

    assert observed["interval_seconds"] == 23
    assert observed["service_is_fake"] == 1
    assert observed["stop_event_created"] == 1


@pytest.mark.anyio
async def test_refresh_keys_skips_duplicate_claims_across_parallel_workers() -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=10)
    plugin = FakeProviderPlugin("openai", ["gpt-4o"], available=True)
    service, repository = build_key_service(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.AVAILABLE,
                last_refreshed_at=now,
                cached_available=True,
            )
        ],
        plugin,
    )

    first, second = await pytest.importorskip("asyncio").gather(
        service.refresh_keys(),
        service.refresh_keys(),
    )

    assert sum((first, second)) == 1
    assert len(plugin.available_checks) == 1
    assert repository._keys["key-1"].last_refreshed_at is not None


@pytest.mark.anyio
async def test_recover_cooldowns_is_safe_to_call_more_than_once() -> None:
    now = datetime.now(timezone.utc)
    plugin = FakeProviderPlugin("openai", ["gpt-4o"], available=True)
    service, repository = build_key_service(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.RATE_LIMITED,
                cooldown_until=now - timedelta(seconds=1),
                last_refreshed_at=now,
                cached_available=True,
            )
        ],
        plugin,
    )

    first = await service.recover_cooldowns()
    second = await service.recover_cooldowns()

    assert first == 1
    assert second == 0
    assert repository._keys["key-1"].status == KeyStatus.AVAILABLE
    assert repository._keys["key-1"].cooldown_until is None


@pytest.mark.anyio
async def test_refresh_keys_retries_fetch_models_without_status_side_effect() -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=10)

    class _RetryFetchModelsPlugin(FakeProviderPlugin):
        def __init__(self) -> None:
            super().__init__("openai", ["gpt-4o"], available=True)
            self.fetch_attempts = 1

        async def fetch_models(self, credential: dict[str, str]) -> list[str]:
            self.fetch_attempts += 1
            if self.fetch_attempts == 1:
                raise RuntimeError("temporary model sync failure")
            return ["gpt-4o"]

    plugin = _RetryFetchModelsPlugin()
    service, repository = build_key_service(
        [
            ApiKey(
                id="key-1",
                provider="openai",
                credential={"api_key": "sk-test"},
                status=KeyStatus.AVAILABLE,
                supported_models=[],
                last_refreshed_at=now,
                cached_available=True,
            )
        ],
        plugin,
    )

    refreshed = await service.refresh_keys()

    assert refreshed == 1
    assert plugin.fetch_attempts == 2
    assert repository._keys["key-1"].status == KeyStatus.AVAILABLE
    assert repository._keys["key-1"].supported_models == ["gpt-4o"]
    assert repository._keys["key-1"].cached_available is True
