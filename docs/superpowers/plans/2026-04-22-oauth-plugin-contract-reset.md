# OAuth Plugin Contract Reset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reset `codex_oauth` and `gemini_oauth` back to the normal provider contract so `KeyService` runs `is_credential_available -> get_capacity_signal -> _sync_models`, while OAuth providers add one auth-type-gated preflight sequence.

**Architecture:** Remove the runtime-credential side channel from `base.py` and `KeyService`, then let `KeyService` run an OAuth preflight sequence using provider-local `_is_oauth_credential_fresh` and `_refresh_oauth_credential` methods. `codex_oauth` keeps a fixed model table and validates with `wham/usage`; `gemini_oauth` treats `retrieveUserQuota` as the primary source for availability, capacity, and dynamic model discovery, while `project_id` remains a runtime-only parameter and is never persisted into credential payloads.

**Tech Stack:** Python, pytest, FastAPI service layer patterns already used in `KeyService`, httpx-based provider plugins

---

## File Structure

### Files To Modify

- `src/infrastructure/plugins/base.py`
  Responsibility: keep only the shared provider contract used by normal plugins; remove runtime credential bypass APIs.
- `src/application/services/key_service.py`
  Responsibility: run the unified refresh pipeline, split api-key vs OAuth by `plugin.auth_type`, persist refreshed OAuth credentials, and keep model sync as the third formal step.
- `src/infrastructure/plugins/providers/codex_oauth.py`
  Responsibility: expose provider-local OAuth freshness handling, keep fixed models, use `wham/usage` for credential validity and capacity.
- `src/infrastructure/plugins/providers/gemini_oauth.py`
  Responsibility: expose provider-local OAuth freshness handling, keep `project_id` runtime-only, use `retrieveUserQuota` for dynamic model list and capacity, and use fallback probe only when quota fetch is unavailable.
- `tests/fakes.py`
  Responsibility: add fake OAuth plugin helpers for `KeyService` tests without reintroducing runtime bypass concepts.
- `tests/test_domain.py`
  Responsibility: lock `KeyService` behavior for api-key path, OAuth preflight sequence path, refresh failure path, and pending validation model sync.
- `tests/test_provider_plugins.py`
  Responsibility: replace runtime-credential tests with direct provider contract tests for `codex_oauth` and `gemini_oauth`.

### Files To Check While Implementing

- `src/infrastructure/plugins/providers/openai.py`
  Reference for the normal api-key provider contract.
- `src/infrastructure/plugins/providers/gemini_openai.py`
  Reference for normal api-key model sync flow.
- `src/infrastructure/plugins/providers/gemini_web_proxy.py`
  Reference for another non-OAuth plugin path that should not be dragged into OAuth preflight logic.

## Task 1: Remove Runtime Bypass From The Base Contract

**Files:**
- Modify: `src/infrastructure/plugins/base.py`
- Test: `tests/test_provider_plugins.py`

- [ ] **Step 1: Write the failing contract cleanup test notes into `tests/test_provider_plugins.py`**

```python
def test_provider_base_no_longer_exposes_runtime_bypass_methods() -> None:
    from infrastructure.plugins.base import ProviderPlugin

    assert not hasattr(ProviderPlugin, "prepare_runtime_credential")
    assert not hasattr(ProviderPlugin, "is_runtime_credential_available")
    assert not hasattr(ProviderPlugin, "get_runtime_capacity_signal")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_provider_plugins.py -k "runtime_bypass_methods" -v`
Expected: FAIL because `ProviderPlugin` still exposes the runtime bypass methods.

- [ ] **Step 3: Remove the runtime bypass API from `src/infrastructure/plugins/base.py`**

```python
@dataclass(slots=True)
class CredentialPreparationResult:
    credential: CredentialDict
    changed: bool = False


class ProviderPlugin(ABC):
    async def prepare_credential(self, credential: CredentialDict) -> CredentialPreparationResult:
        """Return the locally normalized credential to persist.

        This hook is used by registration/update flows. Implementations must not perform
        remote IO, token refresh, runtime project discovery, or quota probing here.
        """
        return CredentialPreparationResult(credential=credential, changed=False)
```

Delete these blocks entirely:

```python
@dataclass(slots=True)
class RuntimeCredentialResult:
    persisted_credential: CredentialDict
    runtime_credential: CredentialDict
    changed: bool = False

async def prepare_runtime_credential(
    self,
    credential: CredentialDict,
) -> RuntimeCredentialResult:
    raise NotImplementedError

async def is_runtime_credential_available(
    self,
    runtime_credential: CredentialDict,
    model: str | None = None,
) -> bool:
    raise NotImplementedError

async def get_runtime_capacity_signal(
    self,
    runtime_credential: CredentialDict,
) -> CapacitySignal | None:
    raise NotImplementedError
```

- [ ] **Step 4: Rewrite the affected provider-plugin tests so they stop referencing runtime bypass names**

```python
def test_provider_base_no_longer_exposes_runtime_bypass_methods() -> None:
    from infrastructure.plugins.base import ProviderPlugin

    assert hasattr(ProviderPlugin, "prepare_credential")
    assert not hasattr(ProviderPlugin, "prepare_runtime_credential")
    assert not hasattr(ProviderPlugin, "is_runtime_credential_available")
    assert not hasattr(ProviderPlugin, "get_runtime_capacity_signal")
```

- [ ] **Step 5: Run the focused tests**

Run: `pytest tests/test_provider_plugins.py -k "runtime_bypass_methods" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/infrastructure/plugins/base.py tests/test_provider_plugins.py
git commit -m "refactor: remove runtime bypass from provider base contract"
```

## Task 2: Rebuild KeyService Around auth_type Routing

**Files:**
- Modify: `src/application/services/key_service.py`
- Modify: `tests/fakes.py`
- Test: `tests/test_domain.py`

- [ ] **Step 1: Write the failing KeyService tests**

Add these tests to `tests/test_domain.py`:

```python
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

    assert plugin.available_checks == [({"api_key": "sk-new"}, None)]
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
    assert plugin.available_checks[0][0]["access_token"] == "new-token"


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
    assert plugin.available_checks == []


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
```

- [ ] **Step 2: Run the failing KeyService tests**

Run: `pytest tests/test_domain.py -k "formal_three_steps or refreshes_first or refresh_fails or create_key_oauth_refresh_failure or validate_pending_key_runs_model_sync" -v`
Expected: FAIL because `create_key()` still skips the formal three-step path, runtime bypass helpers are still used, and `validate_pending_key()` still skips `_sync_models()`.

- [ ] **Step 3: Add OAuth fake support in `tests/fakes.py`**

```python
class FakeOauthProviderPlugin(FakeProviderPlugin):
    def __init__(
        self,
        name: str,
        models: list[str] | None = None,
        *,
        fresh: bool = True,
        refreshed_credential: dict[str, str] | None = None,
        refresh_result: dict[str, str] | None = None,
        available: bool = True,
        capacity_signal: CapacitySignal | None = None,
    ) -> None:
        super().__init__(name, models, available=available, capacity_signal=capacity_signal)
        self._fresh = fresh
        self._refreshed_credential = refreshed_credential
        self._refresh_result = refresh_result
        self.fresh_checks: list[dict[str, str]] = []
        self.refresh_calls: list[dict[str, str]] = []

    @property
    def auth_type(self) -> str:
        return "oauth_json"

    def _is_oauth_credential_fresh(self, credential: dict[str, str]) -> bool:
        self.fresh_checks.append(dict(credential))
        return self._fresh

    async def _refresh_oauth_credential(self, credential: dict[str, str]) -> dict[str, str] | None:
        self.refresh_calls.append(dict(credential))
        if self._refresh_result is None:
            return dict(self._refreshed_credential) if self._refreshed_credential is not None else None
        return dict(self._refresh_result)
```

- [ ] **Step 4: Replace runtime bypass flow in `src/application/services/key_service.py`**

Use this structure:

```python
async def _run_key_preflight(self, key: ApiKey, plugin) -> bool:
    auth_type = (plugin.auth_type or "").lower()
    if "oauth" not in auth_type:
        return True

    if plugin._is_oauth_credential_fresh(key.credential):
        return True

    refreshed = await plugin._refresh_oauth_credential(key.credential)
    if refreshed is None:
        return False
    if not self._credential_equals(refreshed, key.credential):
        key.credential = refreshed
    return True


async def _refresh_single_key(
    self,
    key: ApiKey,
    now: datetime,
    plugin=None,
    model: str | None = None,
) -> bool:
    plugin = plugin or self._provider_registry.get(key.provider)
    if plugin is None:
        key.cached_available = False
        key.cached_quota_available = None
        key.cached_capacity_score = None
        key.last_refreshed_at = now
        self._merge_refresh_signals_into_status(key, now)
        return False

    preflight_ok = await self._run_key_preflight(key, plugin)
    if not preflight_ok:
        key.cached_available = False
        key.cached_quota_available = None
        key.cached_capacity_score = None
        key.last_refreshed_at = now
        self._merge_refresh_signals_into_status(key, now)
        return False

    try:
        key.cached_available = await plugin.is_credential_available(key.credential, model)
    except Exception as exc:
        logger.warning("is_credential_available failed for %s: %s", key.id, exc)
        key.cached_available = False

    if key.cached_available is False:
        key.cached_quota_available = None
        key.cached_capacity_score = None
        key.last_refreshed_at = now
        self._merge_refresh_signals_into_status(key, now)
        return False

    try:
        signal = await plugin.get_capacity_signal(key.credential)
    except Exception as exc:
        logger.warning("get_capacity_signal failed for %s: %s", key.id, exc)
        signal = None

    key.cached_quota_available = None if signal is None else signal.quota_available
    key.cached_capacity_score = None if signal is None else signal.capacity_score
    key.last_refreshed_at = now
    self._merge_refresh_signals_into_status(key, now)
    return True
```

Also change `create_key()` so api-key and OAuth both run the same formal control flow after `verify_upstream_root_reachable()`:

```python
key = ApiKey(
    id=str(uuid4()),
    provider=provider,
    credential=credential,
    status=KeyStatus.PENDING,
    last_refreshed_at=now,
)
should_sync_models = await self._refresh_single_key(key, now, plugin=plugin)
if should_sync_models:
    await self._sync_models(key, plugin=plugin)
await self._repository.upsert_key(key)
await self._allocation_store.sync_key(key, self._scorer.score(key, now))
if key.status == KeyStatus.PENDING:
    self._schedule_pending_validation(key.id)
return key
```

The critical rule for the preflight branch is:

```python
assert should_sync_models is False
assert key.status == KeyStatus.DISABLED_UPSTREAM
assert plugin.available_checks == []
```

Meaning:

```python
if preflight fails:
    # credential is treated as unavailable now
    # write status=disabled_upstream to storage
    # stop here
    # do not call is_credential_available
    # do not call get_capacity_signal
    # do not call _sync_models
```

And change `validate_pending_key()`:

```python
should_sync_models = await self._refresh_single_key(key, now, plugin=plugin)
if should_sync_models:
    await self._sync_models(key, plugin=plugin)
persisted = await self._persist_runtime_key(key, now)
```

Apply the same guard to every service path that currently does `_refresh_single_key(...)` followed by `_sync_models(...)`, including:

```python
create_key(...)
update_key(...)
refresh_keys(...)
validate_pending_key(...)
```

Delete these helpers entirely:

```python
async def _prepare_runtime_key_credential(
    self,
    key: ApiKey,
    plugin,
) -> dict[str, str]:
    raise NotImplementedError

async def _probe_runtime_credential_available(
    self,
    key: ApiKey,
    plugin,
    runtime_credential: dict[str, str],
    model: str | None,
) -> bool:
    raise NotImplementedError

async def _probe_runtime_capacity(
    self,
    key: ApiKey,
    plugin,
    runtime_credential: dict[str, str],
) -> tuple[bool | None, float | None]:
    raise NotImplementedError
```

- [ ] **Step 5: Run the KeyService tests**

Run: `pytest tests/test_domain.py -k "formal_three_steps or refreshes_first or refresh_fails or create_key_oauth_refresh_failure or validate_pending_key_runs_model_sync" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/application/services/key_service.py tests/fakes.py tests/test_domain.py
git commit -m "refactor: route key service by auth type without runtime bypass"
```

## Task 3: Rebuild Codex OAuth Around The Normal Provider Contract

**Files:**
- Modify: `src/infrastructure/plugins/providers/codex_oauth.py`
- Test: `tests/test_provider_plugins.py`

- [ ] **Step 1: Write the failing Codex OAuth tests**

Add or replace these tests in `tests/test_provider_plugins.py`:

```python
@pytest.mark.anyio
async def test_codex_oauth_refresh_oauth_credential_refreshes_only_when_not_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _jwt(payload: dict) -> str:
        import base64
        import json

        def _part(value: dict) -> str:
            raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
            return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

        return f"{_part({'alg': 'none', 'typ': 'JWT'})}.{_part(payload)}.signature"

    fixed_now = datetime(2026, 4, 14, 8, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("infrastructure.plugins.providers.codex_oauth._utc_now", lambda: fixed_now)

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {
                "access_token": "new-access",
                "id_token": _jwt(
                    {
                        "email": "new@example.com",
                        "sub": "new-sub",
                        "https://api.openai.com/auth": {"chatgpt_account_id": "new-account"},
                    }
                ),
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(
        "infrastructure.plugins.providers.codex_oauth.httpx.AsyncClient",
        lambda **kwargs: _FakeClient(),
    )

    plugin = CodexOauthPlugin()
    assert plugin._is_oauth_credential_fresh(
        {
            "access_token": "old-access",
            "id_token": _jwt(
                {
                    "email": "old@example.com",
                    "sub": "old-sub",
                    "https://api.openai.com/auth": {"chatgpt_account_id": "old-account"},
                }
            ),
            "refresh_token": "old-refresh",
            "expired": "2026-04-14T08:06:00Z",
        }
    ) is True

    refreshed = await plugin._refresh_oauth_credential(
        {
            "access_token": "old-access",
            "id_token": _jwt(
                {
                    "email": "old@example.com",
                    "sub": "old-sub",
                    "https://api.openai.com/auth": {"chatgpt_account_id": "old-account"},
                }
            ),
            "refresh_token": "old-refresh",
            "expired": "2026-04-14T08:04:00Z",
        }
    )

    assert refreshed is not None
    assert refreshed["access_token"] == "new-access"
    assert refreshed["refresh_token"] == "new-refresh"


@pytest.mark.anyio
async def test_codex_oauth_availability_uses_wham_usage_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeResponse:
        status_code = 200
        is_success = True

        @staticmethod
        def json() -> dict:
            return {"plan_type": "plus", "rate_limit": {"primary_window": {"used_percent": 100}}}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(
        "infrastructure.plugins.providers.codex_oauth.httpx.AsyncClient",
        lambda **kwargs: _FakeClient(),
    )

    plugin = CodexOauthPlugin()
    assert await plugin.is_credential_available({"access_token": "access", "account_id": "acct"}) is True
```

- [ ] **Step 2: Run the failing Codex tests**

Run: `pytest tests/test_provider_plugins.py -k "codex_oauth" -v`
Expected: FAIL because the file still depends on `prepare_runtime_credential`, `is_runtime_credential_available`, and `get_runtime_capacity_signal`.

- [ ] **Step 3: Refactor `src/infrastructure/plugins/providers/codex_oauth.py`**

Keep `prepare_credential()` local-only, add provider-local OAuth preflight, and remove runtime bypass methods:

```python
def _is_oauth_credential_fresh(self, credential: dict[str, str]) -> bool:
    normalized = self._normalize_credential(credential)
    return not (self._is_near_expiry(normalized) and normalized.get("refresh_token"))


async def _refresh_oauth_credential(self, credential: dict[str, str]) -> dict[str, str] | None:
    try:
        normalized = self._normalize_credential(credential)
        return await self._refresh_credential(normalized)
    except Exception:
        return None


async def prepare_credential(self, credential: dict[str, str]) -> CredentialPreparationResult:
    normalized = self._normalize_credential(credential)
    return CredentialPreparationResult(
        credential=normalized,
        changed=normalized != credential,
    )


async def fetch_models(self, credential: dict[str, str]) -> list[str]:
    fast_models = [f"{m}-fast" for m in _BASE_MODELS]
    return list(dict.fromkeys([*_BASE_MODELS, *fast_models]))


async def is_credential_available(self, credential: dict[str, str], model: str | None = None) -> bool:
    response = await self._retrieve_usage(credential)
    return response is not None and response.is_success


async def get_capacity_signal(self, credential: dict[str, str]) -> CapacitySignal | None:
    response = await self._retrieve_usage(credential)
    if response is None or not response.is_success:
        return None
    payload = response.json()
    if not isinstance(payload, dict):
        return None
    return self._capacity_signal_from_usage_payload(payload)
```

Delete these methods entirely:

```python
async def _prepare_runtime_credential(self, credential: dict[str, str]) -> dict[str, str]:
    raise NotImplementedError

async def prepare_runtime_credential(self, credential: dict[str, str]):
    raise NotImplementedError

async def is_runtime_credential_available(
    self,
    runtime_credential: dict[str, str],
    model: str | None = None,
) -> bool:
    raise NotImplementedError

async def get_runtime_capacity_signal(self, runtime_credential: dict[str, str]) -> CapacitySignal | None:
    raise NotImplementedError
```

- [ ] **Step 4: Replace the old runtime tests with direct contract tests**

Use direct method calls like:

```python
assert await plugin.is_credential_available(
    {
        "access_token": "access-token",
        "account_id": "account-id",
    }
) is True

signal = await plugin.get_capacity_signal(
    {
        "access_token": "access-token",
        "account_id": "account-id",
    }
)
assert signal is not None
assert signal.quota_available is False
```

- [ ] **Step 5: Run the Codex provider tests**

Run: `pytest tests/test_provider_plugins.py -k "codex_oauth" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/infrastructure/plugins/providers/codex_oauth.py tests/test_provider_plugins.py
git commit -m "refactor: align codex oauth with provider contract"
```

## Task 4: Rebuild Gemini OAuth Around Quota-Driven Models And Runtime-Only project_id

**Files:**
- Modify: `src/infrastructure/plugins/providers/gemini_oauth.py`
- Test: `tests/test_provider_plugins.py`

- [ ] **Step 1: Write the failing Gemini OAuth tests**

Add or replace these tests in `tests/test_provider_plugins.py`:

```python
@pytest.mark.anyio
async def test_gemini_oauth_prepare_credential_never_persists_project_id() -> None:
    plugin = GeminiOauthPlugin()

    result = await plugin.prepare_credential(
        {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "project_id": "project-123",
            "expiry_date": "9999999999999",
        }
    )

    assert "project_id" not in result.credential


@pytest.mark.anyio
async def test_gemini_oauth_fetch_models_falls_back_to_fixed_models_only_when_quota_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        def __init__(self, status_code: int, payload: dict):
            self.status_code = status_code
            self.is_success = 200 <= status_code < 300
            self._payload = payload

        def json() -> dict:
            return self._payload

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *args, **kwargs) -> _FakeResponse:
            if url.endswith(":retrieveUserQuota"):
                return _FakeResponse(403, {"error": {"message": "forbidden"}})
            if url.endswith(":loadCodeAssist"):
                return _FakeResponse(200, {"cloudaicompanionProject": "project-123"})
            raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(
        "infrastructure.plugins.providers.gemini_oauth.httpx.AsyncClient",
        lambda **kwargs: _FakeClient(),
    )

    plugin = GeminiOauthPlugin()
    models = await plugin.fetch_models(
        {
            "access_token": "access",
            "refresh_token": "refresh",
            "expiry_date": "9999999999999",
        }
    )

    assert models == plugin._fallback_models()
```

- [ ] **Step 2: Run the failing Gemini tests**

Run: `pytest tests/test_provider_plugins.py -k "gemini_oauth" -v`
Expected: FAIL because the file still uses runtime-credential methods and `fetch_models()` still returns `[]` instead of fallback models when quota is unavailable.

- [ ] **Step 3: Refactor `src/infrastructure/plugins/providers/gemini_oauth.py`**

Keep `project_id` runtime-only and move OAuth freshness into a provider-local preflight:

```python
def _persist_credential(self, credential: dict[str, str]) -> dict[str, str]:
    persisted = dict(credential)
    persisted.pop("project_id", None)
    return persisted


def _is_oauth_credential_fresh(self, credential: dict[str, Any]) -> bool:
    normalized = self._normalize_credential(credential)
    return not self._is_near_expiry(normalized)


async def _refresh_oauth_credential(self, credential: dict[str, Any]) -> dict[str, str] | None:
    try:
        normalized = self._normalize_credential(credential)
        refreshed = await self._refresh_credential(normalized)
    except Exception:
        return None
    return self._persist_credential(refreshed)


async def _build_runtime_credential(self, credential: dict[str, Any]) -> dict[str, str]:
    runtime_credential = dict(self._normalize_credential(credential))
    project_id = str(credential.get("project_id") or "").strip()
    runtime_credential["project_id"] = project_id or await self._discover_project_id(runtime_credential)
    return runtime_credential


def _fallback_models(self) -> list[str]:
    return [
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    ]
```

Then implement the formal contract directly:

```python
async def fetch_models(self, credential: dict[str, str]) -> list[str]:
    runtime_credential = await self._build_runtime_credential(credential)
    response = await self._retrieve_user_quota(runtime_credential)
    if response is None or not response.is_success:
        return self._fallback_models()

    payload = response.json()
    if not isinstance(payload, dict):
        return self._fallback_models()

    models = self._available_models_from_quota_payload(payload)
    if models is None:
        return self._fallback_models()
    return models


async def is_credential_available(self, credential: dict[str, str], model: str | None = None) -> bool:
    runtime_credential = await self._build_runtime_credential(credential)
    response = await self._retrieve_user_quota(runtime_credential)
    if response is not None and response.is_success:
        payload = response.json()
        if isinstance(payload, dict) and self._quota_models_from_payload(payload) is not None:
            return True
    probe_model = model or next(iter(self._fallback_models()), "")
    return await self._probe_generate_content(runtime_credential, probe_model)


async def get_capacity_signal(self, credential: dict[str, str]) -> CapacitySignal | None:
    runtime_credential = await self._build_runtime_credential(credential)
    response = await self._retrieve_user_quota(runtime_credential)
    if response is None or not response.is_success:
        return None

    payload = response.json()
    if not isinstance(payload, dict):
        return None
    return self._quota_signal_from_payload(payload)
```

Delete these methods entirely:

```python
async def _prepare_runtime_credential(self, credential: dict[str, Any]) -> dict[str, str]:
    raise NotImplementedError

async def prepare_runtime_credential(self, credential: dict[str, str]):
    raise NotImplementedError

async def is_runtime_credential_available(
    self,
    runtime_credential: dict[str, str],
    model: str | None = None,
) -> bool:
    raise NotImplementedError

async def get_runtime_capacity_signal(self, runtime_credential: dict[str, str]) -> CapacitySignal | None:
    raise NotImplementedError
```

- [ ] **Step 4: Rewrite Gemini tests around the final semantics**

Use direct assertions like:

```python
models = await plugin.fetch_models(
    {
        "access_token": "access",
        "refresh_token": "refresh",
        "expiry_date": "9999999999999",
    }
)
assert models == ["gemini-2.5-flash", "gemini-2.5-pro", "new-quota-model"]

signal = await plugin.get_capacity_signal(
    {
        "access_token": "access",
        "refresh_token": "refresh",
        "expiry_date": "9999999999999",
    }
)
assert signal is not None
assert signal.quota_available is False
```

Also keep the runtime-only `project_id` rule explicit:

```python
result = await plugin.prepare_credential(
    {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "project_id": "project-123",
        "expiry_date": "9999999999999",
    }
)
assert "project_id" not in result.credential
```

- [ ] **Step 5: Run the Gemini provider tests**

Run: `pytest tests/test_provider_plugins.py -k "gemini_oauth" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/infrastructure/plugins/providers/gemini_oauth.py tests/test_provider_plugins.py
git commit -m "refactor: align gemini oauth with quota-driven provider contract"
```

## Task 5: Run Focused Regression Coverage

**Files:**
- Modify: `tests/test_domain.py`
- Modify: `tests/test_provider_plugins.py`

- [ ] **Step 1: Add one regression test that proves OAuth refresh failure never continues into capacity/model sync**

```python
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
        [ApiKey(id="key-1", provider="codex_oauth", credential={"expired": "1"}, status=KeyStatus.AVAILABLE)]
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
```

- [ ] **Step 2: Run the focused regression suite**

Run: `pytest tests/test_domain.py tests/test_provider_plugins.py -k "oauth or runtime_bypass_methods or formal_three_steps or create_key_oauth_refresh_failure or validate_pending_key_runs_model_sync" -v`
Expected: PASS

- [ ] **Step 3: Run the broader safety suite**

Run: `pytest tests/test_domain.py tests/test_provider_plugins.py tests/test_plugin_registry.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_domain.py tests/test_provider_plugins.py
git commit -m "test: cover oauth preflight and provider contract reset"
```

## Self-Review

### Spec Coverage

- Remove runtime bypass from `base.py`: covered by Task 1.
- Make `KeyService` run api-key old logic and OAuth preflight sequence logic by `auth_type`: covered by Task 2.
- Keep refreshed OAuth credentials persisted into `key.credential`: covered by Task 2.
- Keep `project_id` runtime-only and not persisted into Gemini credential payloads: covered by Task 4.
- Keep Codex fixed model list and use `wham/usage` for availability/capacity: covered by Task 3.
- Make Gemini models come from quota, exclude zero-capacity models, and only fallback to fixed list when quota is unavailable: covered by Task 4.
- Ensure refresh failure lands in `disabled_upstream` and stops formal probing: covered by Task 2 and Task 5.

### Placeholder Scan

- No placeholder markers remain.
- Every task has exact file paths, exact test names, and exact commands.

### Type Consistency

- The only OAuth preflight hooks used by the service plan are `_is_oauth_credential_fresh` and `_refresh_oauth_credential`.
- The three formal provider methods remain `fetch_models`, `is_credential_available`, and `get_capacity_signal`.
- `project_id` is consistently treated as runtime-only in Gemini steps and never as a persisted credential field.

Plan complete and saved to `docs/superpowers/plans/2026-04-22-oauth-plugin-contract-reset.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
