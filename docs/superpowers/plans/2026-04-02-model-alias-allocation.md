# Model Alias Allocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add YAML-driven canonical model alias resolution so `allocate-key` and `allocate-by-model` can match provider-native model names and return `provider_model` while keeping the current cross-provider global score ranking behavior.

**Architecture:** Add a read-only `ModelAliasResolver` that loads a YAML file from `MODEL_ALIAS_CONFIG_PATH`, validates the schema, and resolves canonical model names to provider-native aliases at allocation time. Keep `supported_models` stored as provider-native values, keep provider plugins unchanged, and extend allocation results to carry `provider_model` back through the API layer.

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2, Punq DI, PyYAML, pytest

---

## File Structure

- Create: `src/application/services/model_alias_resolver.py`
- Create: `tests/test_model_alias_resolver.py`
- Create: `tests/output/model_alias/valid_with_aliases.yaml`
- Create: `tests/output/model_alias/valid_single_provider.yaml`
- Create: `tests/output/model_alias/invalid_empty_alias_list.yaml`
- Create: `tests/output/model_alias/invalid_models_type.yaml`
- Create: `config/model_aliases.example.yaml`
- Modify: `pyproject.toml`
- Modify: `src/infrastructure/config/settings.py`
- Modify: `src/container/container.py`
- Modify: `src/application/services/key_service.py`
- Modify: `src/interfaces/schemas/response.py`
- Modify: `src/interfaces/api/routes/allocate.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_domain.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `docs/router.md`

### Task 1: Add YAML Dependency, Config Entry, and Sample File

**Files:**
- Create: `tests/test_model_alias_resolver.py`
- Modify: `pyproject.toml`
- Modify: `src/infrastructure/config/settings.py`
- Modify: `.env.example`
- Create: `config/model_aliases.example.yaml`

- [ ] **Step 1: Write the failing config test**

Create `tests/test_model_alias_resolver.py` with:

```python
from infrastructure.config.settings import Settings


def test_settings_exposes_model_alias_config_path() -> None:
    settings = Settings(
        APP_NAME="KeyFlowTest",
        API_PREFIX="/api",
        INTERNAL_API_KEY="test-key",
        DATABASE_URL_READ="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        DATABASE_URL_WRITE="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        REDIS_URL="redis://localhost:6379/9",
        MODEL_ALIAS_CONFIG_PATH="/config/model_aliases.yaml",
    )

    assert settings.model_alias_config_path == "/config/model_aliases.yaml"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_model_alias_resolver.py::test_settings_exposes_model_alias_config_path -v`
Expected: FAIL because `Settings` has no `model_alias_config_path` field.

- [ ] **Step 3: Add YAML dependency and settings field**

`pyproject.toml`

```toml
dependencies = [
  "fastapi>=0.115.0",
  "pydantic>=2.8.0",
  "pydantic-settings>=2.4.0",
  "punq>=0.7.0",
  "redis>=5.0.0",
  "sqlalchemy[asyncio]>=2.0.0",
  "asyncpg>=0.29.0",
  "uvicorn[standard]>=0.30.0",
  "httpx>=0.27.0",
  "PyYAML>=6.0.2"
]
```

`src/infrastructure/config/settings.py`

```python
    model_alias_config_path: str | None = Field(default=None, alias="MODEL_ALIAS_CONFIG_PATH")
```

`.env.example`

```env
MODEL_ALIAS_CONFIG_PATH=
```

`config/model_aliases.example.yaml`

```yaml
version: 1

models:
  gpt-4o:
    providers:
      openai:
        - gpt-4o
      openrouter:
        - openai/gpt-4o
        - openai/gpt-4o-2024-11-20

  claude-3-7-sonnet:
    providers:
      anthropic:
        - claude-3-7-sonnet-20250219
      openrouter:
        - anthropic/claude-3.7-sonnet

  gemini-2.5-pro:
    providers:
      gemini:
        - gemini-2.5-pro
      openrouter:
        - google/gemini-2.5-pro
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_model_alias_resolver.py::test_settings_exposes_model_alias_config_path -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/infrastructure/config/settings.py .env.example config/model_aliases.example.yaml tests/test_model_alias_resolver.py
git commit -m "feat: add model alias config settings"
```

### Task 2: Implement the YAML Model Alias Resolver

**Files:**
- Create: `src/application/services/model_alias_resolver.py`
- Modify: `tests/test_model_alias_resolver.py`
- Create: `tests/output/model_alias/valid_with_aliases.yaml`
- Create: `tests/output/model_alias/valid_single_provider.yaml`
- Create: `tests/output/model_alias/invalid_empty_alias_list.yaml`
- Create: `tests/output/model_alias/invalid_models_type.yaml`

- [ ] **Step 1: Write the failing resolver tests**

```python
from pathlib import Path

import pytest

from application.services.model_alias_resolver import ModelAliasResolver

FIXTURES = Path("tests/output/model_alias")


def test_resolver_matches_provider_aliases() -> None:
    resolver = ModelAliasResolver.from_yaml_file(str(FIXTURES / "valid_with_aliases.yaml"))

    match = resolver.resolve_provider_model(
        requested_model="gpt-4o",
        provider="openrouter",
        supported_models=["openai/gpt-4o-2024-11-20"],
    )

    assert match == "openai/gpt-4o-2024-11-20"


def test_resolver_falls_back_to_requested_model_when_not_configured() -> None:
    resolver = ModelAliasResolver.empty()

    match = resolver.resolve_provider_model(
        requested_model="gpt-4o-mini",
        provider="openai",
        supported_models=["gpt-4o-mini"],
    )

    assert match == "gpt-4o-mini"


def test_resolver_returns_first_alias_when_models_not_synced() -> None:
    resolver = ModelAliasResolver.from_yaml_file(str(FIXTURES / "valid_single_provider.yaml"))

    match = resolver.resolve_provider_model(
        requested_model="gpt-4o",
        provider="openrouter",
        supported_models=[],
    )

    assert match == "openai/gpt-4o"


def test_resolver_rejects_invalid_schema() -> None:
    with pytest.raises(ValueError, match="must define at least one alias"):
        ModelAliasResolver.from_yaml_file(str(FIXTURES / "invalid_empty_alias_list.yaml"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_model_alias_resolver.py -v`
Expected: FAIL because `ModelAliasResolver` does not exist.

- [ ] **Step 3: Write the minimal resolver**

Create the YAML examples as static fixture files under `tests/output/model_alias/` instead of generating them at runtime. The current execution environment may reject `tmp_path.write_text(...)`, so the plan should not depend on temporary file writes for these tests.

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class ModelAliasResolver:
    aliases_by_model: dict[str, dict[str, list[str]]]

    @classmethod
    def empty(cls) -> "ModelAliasResolver":
        return cls(aliases_by_model={})

    @classmethod
    def from_yaml_file(cls, path: str | None) -> "ModelAliasResolver":
        if not path:
            return cls.empty()

        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if data.get("version") != 1:
            raise ValueError("model alias config version must be 1")

        models = data.get("models")
        if not isinstance(models, dict):
            raise ValueError("model alias config models must be a mapping")

        normalized: dict[str, dict[str, list[str]]] = {}
        for requested_model, config in models.items():
            providers = (config or {}).get("providers")
            if not isinstance(providers, dict) or not providers:
                raise ValueError(f"model {requested_model} must define providers")

            provider_aliases: dict[str, list[str]] = {}
            for provider, aliases in providers.items():
                if not isinstance(aliases, list) or not aliases:
                    raise ValueError(f"provider {provider} must define at least one alias")
                alias_list = [str(alias).strip() for alias in aliases if str(alias).strip()]
                if not alias_list:
                    raise ValueError(f"provider {provider} must define at least one alias")
                provider_aliases[str(provider).strip().lower()] = alias_list

            normalized[str(requested_model).strip().lower()] = provider_aliases

        return cls(aliases_by_model=normalized)

    def resolve_provider_model(
        self,
        requested_model: str,
        provider: str,
        supported_models: list[str],
    ) -> str | None:
        normalized_model = requested_model.strip().lower()
        normalized_provider = provider.strip().lower()
        configured_aliases = self.aliases_by_model.get(normalized_model, {}).get(normalized_provider)

        if configured_aliases is None:
            if supported_models and requested_model not in supported_models:
                return None
            return requested_model

        if not supported_models:
            return configured_aliases[0]

        supported_lookup = {model.lower(): model for model in supported_models}
        for alias in configured_aliases:
            matched = supported_lookup.get(alias.lower())
            if matched is not None:
                return matched
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_model_alias_resolver.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/application/services/model_alias_resolver.py tests/test_model_alias_resolver.py tests/output/model_alias
git commit -m "feat: add yaml model alias resolver"
```

### Task 3: Inject the Resolver Through the Container

**Files:**
- Modify: `src/container/container.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write the failing DI test**

```python
FIXTURES = Path("tests/output/model_alias")


def test_build_test_client_supports_model_alias_config() -> None:
    alias_path = FIXTURES / "valid_single_provider.yaml"

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
            )
        ]
    )
    plugin = FakeProviderPlugin("openrouter", ["openai/gpt-4o"], available=True)
    client = build_test_client(repository=repository, plugins=[plugin], model_alias_config_path=str(alias_path))

    response = client.post(
        "/api/internal/allocate-by-model",
        json={"model": "gpt-4o"},
        headers={"X-Internal-Key": "test-key"},
    )

    assert response.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api.py::test_build_test_client_supports_model_alias_config -v`
Expected: FAIL because `build_test_client` does not accept `model_alias_config_path`.

- [ ] **Step 3: Wire the resolver into container and test helpers**

`src/container/container.py`

```python
from application.services.model_alias_resolver import ModelAliasResolver
```

```python
    model_alias_resolver = ModelAliasResolver.from_yaml_file(settings.model_alias_config_path)
    service = KeyService(
        repository,
        allocation_store,
        scheduler,
        scorer,
        state_machine,
        provider_registry,
        model_alias_resolver=model_alias_resolver,
        allocation_lease_seconds=settings.allocate_lease_seconds,
        refresh_cache_seconds=settings.refresh_cache_seconds,
    )
```

```python
    container.register(ModelAliasResolver, instance=model_alias_resolver)
```

`tests/test_api.py`

```python
def build_test_client(
    *,
    repository: InMemoryKeyRepository,
    plugins: list[FakeProviderPlugin],
    allocation_store: InMemoryAllocationStore | None = None,
    health_checkers: dict[str, HealthCheckFn] | None = None,
    model_alias_config_path: str | None = None,
) -> TestClient:
    resolved_allocation_store = allocation_store or InMemoryAllocationStore()
    scorer = KeyScorer()
    scheduler = KeyScheduler(scorer, jitter=0.0)
    state_machine = KeyStateMachine()
    provider_registry = build_provider_registry(*plugins)
    resolver = ModelAliasResolver.from_yaml_file(model_alias_config_path)
    service = KeyService(
        repository,
        resolved_allocation_store,
        scheduler,
        scorer,
        state_machine,
        provider_registry,
        model_alias_resolver=resolver,
    )
```

Add startup failure tests:

```python
def test_create_app_fails_when_model_alias_file_is_missing() -> None:
    settings = Settings(
        APP_NAME="KeyFlowTest",
        API_PREFIX="/api",
        INTERNAL_API_KEY="test-key",
        DATABASE_URL_READ="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        DATABASE_URL_WRITE="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        REDIS_URL="redis://localhost:6379/9",
        MODEL_ALIAS_CONFIG_PATH="Z:/missing/model_aliases.yaml",
    )

    with pytest.raises(FileNotFoundError):
        create_app(settings=settings)


def test_build_test_client_fails_when_model_alias_yaml_is_invalid() -> None:
    repository = InMemoryKeyRepository([])
    plugin = FakeProviderPlugin("openai", ["gpt-4o"], available=True)

    with pytest.raises(ValueError, match="models must be a mapping"):
        build_test_client(
            repository=repository,
            plugins=[plugin],
            model_alias_config_path=str(FIXTURES / "invalid_models_type.yaml"),
        )
```

Startup contract:

```text
1. MODEL_ALIAS_CONFIG_PATH not set -> use empty resolver and continue startup
2. MODEL_ALIAS_CONFIG_PATH set but file missing -> startup fails immediately
3. MODEL_ALIAS_CONFIG_PATH set but YAML invalid -> startup fails immediately
4. MODEL_ALIAS_CONFIG_PATH set but file unreadable -> follows the same startup-failure path, but this plan explicitly verifies only missing-file and invalid-YAML cases
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_api.py::test_build_test_client_supports_model_alias_config tests/test_api.py::test_create_app_fails_when_model_alias_file_is_missing tests/test_api.py::test_build_test_client_fails_when_model_alias_yaml_is_invalid -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/container/container.py tests/test_api.py
git commit -m "feat: inject model alias resolver"
```

### Task 4: Make KeyService Return `provider_model`

**Files:**
- Modify: `src/application/services/key_service.py`
- Modify: `tests/test_domain.py`

- [ ] **Step 1: Write the failing service tests**

```python
@pytest.mark.asyncio
async def test_allocate_by_model_returns_provider_model_from_alias() -> None:
    resolver = ModelAliasResolver.from_yaml_file(
        "tests/output/model_alias/valid_single_provider.yaml"
    )
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
        model_alias_resolver=resolver,
    )

    selected = await service.allocate_key_by_model("gpt-4o")

    assert selected.provider_model == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_allocate_key_falls_back_to_requested_model_when_alias_missing() -> None:
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

    assert selected.provider_model == "gpt-4o-mini"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_domain.py -k "provider_model or alias_missing" -v`
Expected: FAIL because allocation methods return `ApiKey` with no `provider_model`.

- [ ] **Step 3: Introduce an allocation result object and resolver-aware matching**

`src/application/services/key_service.py`

```python
from application.services.model_alias_resolver import ModelAliasResolver
```

```python
@dataclass(slots=True)
class AllocationResult:
    key: ApiKey
    provider_model: str | None = None
```

```python
    async def allocate_key(self, provider: str, model: str | None = None) -> AllocationResult:
```

```python
    async def allocate_key_by_model(self, model: str) -> AllocationResult:
```

```python
        model_alias_resolver: ModelAliasResolver | None = None,
```

```python
        self._model_alias_resolver = model_alias_resolver or ModelAliasResolver.empty()
```

```python
    async def _collect_candidates(
        self,
        keys: list[ApiKey],
        model: str | None,
        now: datetime,
    ) -> tuple[list[ApiKey], dict[str, float | None], dict[str, str | None]]:
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
```

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_domain.py -k "provider_model or alias_missing" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/application/services/key_service.py tests/test_domain.py
git commit -m "feat: return provider model from allocations"
```

### Task 5: Migrate Existing Allocation Call Sites to `AllocationResult`

**Files:**
- Modify: `tests/test_domain.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write the failing migration tests**

Update existing direct service-call assertions so they reflect the new return type.

Representative updates in `tests/test_domain.py`:

```python
    selected = await service.allocate_key_by_model("gpt-4o")

    assert selected.key.id == "openrouter-high"
    assert selected.provider_model == "gpt-4o"
```

```python
    selected = await service.allocate_key_by_model("gpt-4o")

    assert selected.key.id == "anthropic-supported"
    assert selected.provider_model == "gpt-4o"
```

Representative update in `tests/test_api.py`:

```python
    selected = await service.allocate_key_by_model("gpt-4o")

    assert selected.key.id == "openrouter-high"
    assert selected.provider_model == "gpt-4o"
```

Update monkeypatches to return `AllocationResult` when stubbing allocation methods:

```python
from application.services.key_service import AllocationResult, KeyService
```

```python
    async def _fake_allocate_key_by_model(model: str) -> AllocationResult:
        raise NoAvailableKeyError(f"no available key for {model}")
```

- [ ] **Step 2: Run migration-focused tests to verify they fail**

Run: `python -m pytest tests/test_domain.py::test_service_allocate_by_model_prefers_best_key_across_providers tests/test_domain.py::test_service_allocate_by_model_excludes_keys_without_target_model_support tests/test_api.py::test_allocate_by_model_passes_ranked_candidates_to_allocation_store tests/test_api.py::test_allocate_by_model_returns_404_when_no_key_available tests/test_api.py::test_allocate_by_model_returns_provider_and_credential -v`
Expected: FAIL on legacy `selected.id` access or outdated monkeypatch assumptions.

- [ ] **Step 3: Update all direct allocation call sites**

Apply these migration rules consistently:

```text
1. selected.id -> selected.key.id
2. selected.provider -> selected.key.provider
3. selected.credential -> selected.key.credential
4. Add provider_model assertions anywhere the request includes model
5. Monkeypatches and helper fakes must return AllocationResult when stubbing allocate_key / allocate_key_by_model
```

Known locations to update:

```text
tests/test_domain.py:
- test_service_allocate_by_model_prefers_best_key_across_providers
- test_service_allocate_by_model_excludes_keys_without_target_model_support

tests/test_api.py:
- test_allocate_by_model_passes_ranked_candidates_to_allocation_store
- test_allocate_by_model_returns_404_when_no_key_available
- any newly added provider_model route tests
```

- [ ] **Step 4: Update affected existing API assertions**

Do not treat pre-existing API tests as irrelevant. Update any old assertions that now need to reflect the intentionally changed response contract.

Key examples to update in `tests/test_api.py`:

```python
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
    assert payload["provider_model"] is None
    assert payload["credential"] == {"api_key": "sk-test"}
```

```python
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
        "provider_model": "gpt-4o",
        "credential": {"api_key": "sk-test"},
    }
```

Update any other legacy assertions that currently assume the response contains only the old fields.

- [ ] **Step 5: Run migration-focused tests to verify they pass**

Run: `python -m pytest tests/test_domain.py::test_service_allocate_by_model_prefers_best_key_across_providers tests/test_domain.py::test_service_allocate_by_model_excludes_keys_without_target_model_support tests/test_api.py::test_allocate_and_report_cycle tests/test_api.py::test_allocate_by_model_returns_provider_and_credential tests/test_api.py::test_allocate_by_model_passes_ranked_candidates_to_allocation_store tests/test_api.py::test_allocate_by_model_returns_404_when_no_key_available -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tests/test_domain.py tests/test_api.py
git commit -m "test: migrate allocation call sites to allocation result"
```

### Task 6: Expose `provider_model` Through the API

**Files:**
- Modify: `src/interfaces/schemas/response.py`
- Modify: `src/interfaces/api/routes/allocate.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write the failing API response tests**

```python
def test_allocate_by_model_returns_provider_model() -> None:
    alias_path = Path("tests/output/model_alias/valid_single_provider.yaml")
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
            )
        ]
    )
    plugin = FakeProviderPlugin("openrouter", ["openai/gpt-4o"], available=True)
    client = build_test_client(
        repository=repository,
        plugins=[plugin],
        model_alias_config_path=str(alias_path),
    )

    response = client.post(
        "/api/internal/allocate-by-model",
        json={"model": "gpt-4o"},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["provider_model"] == "openai/gpt-4o"


def test_allocate_key_returns_provider_model_when_model_is_given() -> None:
    alias_path = Path("tests/output/model_alias/valid_single_provider.yaml")
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
            )
        ]
    )
    plugin = FakeProviderPlugin("openrouter", ["openai/gpt-4o"], available=True)
    client = build_test_client(
        repository=repository,
        plugins=[plugin],
        model_alias_config_path=str(alias_path),
    )

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openrouter", "model": "gpt-4o"},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["provider_model"] == "openai/gpt-4o"


def test_allocate_key_without_model_returns_null_provider_model() -> None:
    client = build_client()

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai"},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["provider_model"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api.py -k "returns_provider_model" -v`
Expected: FAIL because response schema does not include `provider_model`.

- [ ] **Step 3: Update response models and routes**

`src/interfaces/schemas/response.py`

```python
class AllocateResponse(BaseModel):
    key_id: str
    provider_model: str | None
    credential: dict[str, str]


class AllocateByModelResponse(BaseModel):
    key_id: str
    provider: str
    provider_model: str
    credential: dict[str, str]
```

`src/interfaces/api/routes/allocate.py`

```python
        allocation = await service.allocate_key(payload.provider, payload.model)
```

```python
    return AllocateResponse(
        key_id=allocation.key.id,
        provider_model=allocation.provider_model,
        credential=allocation.key.credential,
    )
```

```python
        allocation = await service.allocate_key_by_model(payload.model)
```

```python
    return AllocateByModelResponse(
        key_id=allocation.key.id,
        provider=allocation.key.provider,
        provider_model=allocation.provider_model or payload.model,
        credential=allocation.key.credential,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_api.py -k "returns_provider_model" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/interfaces/schemas/response.py src/interfaces/api/routes/allocate.py tests/test_api.py
git commit -m "feat: expose provider model in allocation api"
```

### Task 7: Cover Retry-Safe Ranking and Fallback Behavior

**Files:**
- Modify: `tests/test_domain.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write the failing retry and fallback tests**

```python
@pytest.mark.asyncio
async def test_report_error_lowers_score_for_next_retry() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-a",
                provider="openai",
                credential={"api_key": "sk-a"},
                supported_models=["gpt-4o"],
                last_used_at=now,
                last_refreshed_at=now,
                cached_available=True,
            ),
            ApiKey(
                id="key-b",
                provider="openai",
                credential={"api_key": "sk-b"},
                supported_models=["gpt-4o"],
                last_used_at=now - timedelta(seconds=30),
                last_refreshed_at=now,
                cached_available=True,
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
        model_alias_resolver=ModelAliasResolver.empty(),
    )

    first = await service.allocate_key("openai", "gpt-4o")
    await service.report_error(first.key.id, "network_timeout")
    second = await service.allocate_key("openai", "gpt-4o")

    assert second.key.id != first.key.id


def test_allocate_by_model_uses_plain_supported_models_when_alias_missing() -> None:
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
            )
        ]
    )
    plugin = FakeProviderPlugin("openai", ["gpt-4o-mini"], available=True)
    client = build_test_client(repository=repository, plugins=[plugin])

    response = client.post(
        "/api/internal/allocate-by-model",
        json={"model": "gpt-4o-mini"},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["provider_model"] == "gpt-4o-mini"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_domain.py::test_report_error_lowers_score_for_next_retry tests/test_api.py::test_allocate_by_model_uses_plain_supported_models_when_alias_missing -v`
Expected: FAIL until the new allocation result flow and fallback logic are both complete.

- [ ] **Step 3: Keep the behavior explicit**

Do not add a new anti-repeat state machine. Keep the existing self-consistent behavior:

```text
1. report_error increments error_count
2. scorer subtracts error penalty on the next allocation
3. rate_limit, quota_exhausted, and disabled still short-circuit via status
4. generic errors remain AVAILABLE but with a lower score
```

- [ ] **Step 4: Run targeted tests to verify they pass**

Run: `python -m pytest tests/test_domain.py::test_report_error_lowers_score_for_next_retry tests/test_api.py::test_allocate_by_model_uses_plain_supported_models_when_alias_missing -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_domain.py tests/test_api.py
git commit -m "test: cover alias fallback and retry ranking"
```

### Task 8: Update User-Facing Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/router.md`
- Modify: `.env.example`

- [ ] **Step 1: Update README**

```md
## Model Alias Config

KeyFlow can load a YAML model alias file through `MODEL_ALIAS_CONFIG_PATH`.

Example:

```yaml
version: 1

models:
  gpt-4o:
    providers:
      openai:
        - gpt-4o
      openrouter:
        - openai/gpt-4o
        - openai/gpt-4o-2024-11-20
```

Docker example:

```yaml
services:
  keyflow-api:
    volumes:
      - ./config/model_aliases.yaml:/config/model_aliases.yaml:ro
    environment:
      MODEL_ALIAS_CONFIG_PATH: /config/model_aliases.yaml
```

When a request includes `model`, allocation responses now include `provider_model`, which is the provider-native model name that should be sent upstream.

- [ ] **Step 2: Update `docs/router.md`**

Replace the allocation response examples and field descriptions with these exact blocks:

```json
{
  "key_id": "key-1",
  "provider_model": "gpt-4o",
  "credential": {
    "api_key": "sk-test"
  }
}
```

```json
{
  "key_id": "key-openrouter",
  "provider": "openrouter",
  "provider_model": "openai/gpt-4o",
  "credential": {
    "api_key": "sk-or-best"
  }
}
```

```md
- `model`: canonical model name from the request; when YAML mapping exists, match against provider-native model names for the selected provider
- `provider_model`: provider-native model name returned for the selected credential; upstream should use this value when calling the provider API
```


- [ ] **Step 3: Run a focused regression pass**

Run: `python -m pytest tests/test_model_alias_resolver.py -v`
Run: `python -m pytest tests/test_domain.py::test_allocate_by_model_returns_provider_model_from_alias tests/test_domain.py::test_allocate_key_falls_back_to_requested_model_when_alias_missing tests/test_api.py::test_allocate_by_model_returns_provider_model tests/test_api.py::test_allocate_key_returns_provider_model_when_model_is_given tests/test_api.py::test_allocate_key_without_model_returns_null_provider_model tests/test_api.py::test_allocate_by_model_uses_plain_supported_models_when_alias_missing -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add README.md docs/router.md .env.example
git commit -m "docs: document model alias allocation flow"
```

### Task 9: Final Verification

**Files:**
- Modify: none
- Test: `tests/test_model_alias_resolver.py`
- Test: `tests/test_domain.py`
- Test: `tests/test_api.py`

- [ ] **Step 1: Run the full targeted suite**

```bash
python -m pytest tests/test_model_alias_resolver.py tests/test_domain.py tests/test_api.py -q
```

Expected: PASS

- [ ] **Step 2: Run collection sanity check**

```bash
python -m pytest --collect-only -q
```

Expected: collection succeeds and includes the new resolver tests.

- [ ] **Step 3: Review the exposed API contract manually**

```text
1. allocate-key without model returns provider_model = null.
2. allocate-key with canonical model returns provider_model as provider-native name.
3. allocate-by-model returns key_id, provider, provider_model, credential.
4. report-success and report-error contracts remain unchanged.
```

- [ ] **Step 4: Confirm worktree state**

```bash
git status
```

Expected: clean working tree after the planned commits, or only unrelated user-owned changes remain.
