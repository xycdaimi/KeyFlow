# Codex OAuth Provider Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a first-class Codex OAuth provider plugin that accepts OAuth credential JSON, returns static Codex model IDs, refreshes expiring tokens, and exposes normalized quota signals from the WHAM usage endpoint.

**Architecture:** Replace the current API-key-style Codex provider with an OAuth-credential-aware plugin in [`src/infrastructure/plugins/providers/codex.py`](d:/py/keyflow/src/infrastructure/plugins/providers/codex.py), then register that plugin in the provider registry. Keep the plugin boundary thin: JWT parsing, refresh grant, static model expansion, and usage normalization stay inside the plugin; allocation, persistence, and status merge continue to live in the existing service layer.

**Tech Stack:** Python 3.13, `httpx`, pytest, FastAPI admin/provider registry, existing `ProviderPlugin` contract

---

## File Structure

**Create**
- `docs/superpowers/plans/2026-04-09-codex-oauth-provider-plugin.md`

**Modify**
- `src/infrastructure/plugins/providers/codex.py`
- `src/infrastructure/plugins/providers/__init__.py`
- `src/container/container.py`
- `tests/test_provider_plugins.py`
- `tests/test_container_plugins.py`

**Responsibilities**
- `src/infrastructure/plugins/providers/codex.py`: Implement the Codex OAuth provider plugin, including credential normalization, JWT parsing, token refresh, static model listing, availability probing, capacity probing, and admin-safe explanation output.
- `src/infrastructure/plugins/providers/__init__.py`: Export the Codex OAuth plugin so wildcard provider imports remain consistent.
- `src/container/container.py`: Register the Codex OAuth plugin in the provider registry used by the application container.
- `tests/test_provider_plugins.py`: Add focused unit tests for credential hint, static models, refresh behavior, availability checks, quota normalization, and explain payload.
- `tests/test_container_plugins.py`: Verify the container registers the Codex plugin under the expected provider name.

---

### Task 1: Add Provider Registry Coverage For Codex OAuth

**Files:**
- Modify: `src/infrastructure/plugins/providers/__init__.py`
- Modify: `src/container/container.py`
- Test: `tests/test_container_plugins.py`

- [ ] **Step 1: Write the failing container registration test**

```python
from container.container import create_container
from infrastructure.config.settings import Settings
from infrastructure.plugins.base import ProviderRegistry


def _settings() -> Settings:
    return Settings(
        APP_NAME="KeyFlowTest",
        API_PREFIX="/api",
        INTERNAL_API_KEY="test-key",
        DATABASE_URL_READ="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        DATABASE_URL_WRITE="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        REDIS_URL="redis://localhost:6379/9",
    )


def test_container_can_register_codex_oauth_plugin() -> None:
    container = create_container(_settings())
    registry = container.resolve(ProviderRegistry)

    plugin = registry.get("codex")

    assert plugin is not None
    assert plugin.name == "codex"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_container_plugins.py::test_container_can_register_codex_oauth_plugin -v`

Expected: FAIL because the container still registers `OpenAiCodeXPlugin()` instead of the new OAuth plugin, or because `codex.py` is still empty/unexported.

- [ ] **Step 3: Export and register the Codex OAuth plugin**

```python
# src/infrastructure/plugins/providers/__init__.py
from infrastructure.plugins.providers.anthropic import AnthropicPlugin
from infrastructure.plugins.providers.codex import CodexPlugin
from infrastructure.plugins.providers.gemini import GeminiPlugin
from infrastructure.plugins.providers.gemini_web_proxy import GeminiWebProxyPlugin
from infrastructure.plugins.providers.openai import OpenAIPlugin
from infrastructure.plugins.providers.openrouter import OpenRouterPlugin

__all__ = [
    "AnthropicPlugin",
    "CodexPlugin",
    "GeminiPlugin",
    "GeminiWebProxyPlugin",
    "OpenAIPlugin",
    "OpenRouterPlugin",
]
```

```python
# src/container/container.py
from infrastructure.plugins.providers import *

...

    provider_registry.register(OpenRouterPlugin())
    provider_registry.register(GeminiWebProxyPlugin())
    provider_registry.register(CodexPlugin())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_container_plugins.py::test_container_can_register_codex_oauth_plugin -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/infrastructure/plugins/providers/__init__.py src/container/container.py tests/test_container_plugins.py
git commit -m "feat: register codex oauth provider plugin"
```

### Task 2: Implement Credential Shape And Static Model Listing

**Files:**
- Modify: `src/infrastructure/plugins/providers/codex.py`
- Test: `tests/test_provider_plugins.py`

- [ ] **Step 1: Write the failing tests for credential metadata and static models**

```python
import pytest

from infrastructure.plugins.providers.codex import CodexPlugin


def test_codex_plugin_metadata_and_hint() -> None:
    plugin = CodexPlugin()

    assert plugin.name == "codex"
    assert plugin.auth_type == "oauth_json"
    assert plugin.model_source == "static"
    assert plugin.credential_hint == (
        '{"access_token":"...",'
        '"id_token":"...",'
        '"refresh_token":"...",'
        '"account_id":"...",'
        '"email":"...",'
        '"expired":"2026-04-09T13:00:00Z"} '
        '(OpenAI Codex OAuth credential JSON)'
    )


@pytest.mark.anyio
async def test_codex_fetch_models_returns_static_models_with_fast_aliases() -> None:
    plugin = CodexPlugin()

    models = await plugin.fetch_models(
        {
            "access_token": "access-token",
            "id_token": "header.payload.sig",
            "refresh_token": "refresh-token",
            "account_id": "acct_123",
            "email": "user@example.com",
            "expired": "2099-04-09T13:00:00Z",
        }
    )

    assert "gpt-5" in models
    assert "gpt-5-fast" in models
    assert "gpt-5.1-codex-max" in models
    assert "gpt-5.1-codex-max-fast" in models
    assert len(models) == len(set(models))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_provider_plugins.py::test_codex_plugin_metadata_and_hint tests/test_provider_plugins.py::test_codex_fetch_models_returns_static_models_with_fast_aliases -v`

Expected: FAIL because `CodexPlugin` does not yet exist or does not implement the expected metadata/model behavior.

- [ ] **Step 3: Write the minimal Codex plugin skeleton with static model support**

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-09
@Description: OpenAI Codex OAuth 供应商插件
"""
from __future__ import annotations

from infrastructure.plugins.base import ProviderPlugin

_BASE_MODELS: list[str] = [
    "gpt-5",
    "gpt-5-codex",
    "gpt-5-codex-mini",
    "gpt-5.1",
    "gpt-5.1-codex",
    "gpt-5.1-codex-mini",
    "gpt-5.1-codex-max",
    "gpt-5.2",
    "gpt-5.2-codex",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "gpt-5.4",
    "gpt-5.4-mini",
]


class CodexPlugin(ProviderPlugin):
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_INTERFACE_VERSION = "1.0.0"

    @property
    def name(self) -> str:
        return "codex"

    @property
    def description(self) -> str:
        return "OpenAI Codex OAuth provider using ChatGPT backend-api credentials."

    @property
    def auth_type(self) -> str:
        return "oauth_json"

    @property
    def credential_hint(self) -> str:
        return (
            '{"access_token":"...",'
            '"id_token":"...",'
            '"refresh_token":"...",'
            '"account_id":"...",'
            '"email":"...",'
            '"expired":"2026-04-09T13:00:00Z"} '
            '(OpenAI Codex OAuth credential JSON)'
        )

    @property
    def model_source(self) -> str:
        return "static"

    async def fetch_models(self, credential: dict[str, str]) -> list[str]:
        fast_models = [f"{model}-fast" for model in _BASE_MODELS]
        return list(dict.fromkeys([*_BASE_MODELS, *fast_models]))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_provider_plugins.py::test_codex_plugin_metadata_and_hint tests/test_provider_plugins.py::test_codex_fetch_models_returns_static_models_with_fast_aliases -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/infrastructure/plugins/providers/codex.py tests/test_provider_plugins.py
git commit -m "feat: add codex oauth credential metadata and static models"
```

### Task 3: Add Credential Normalization, Refresh, And Availability Checks

**Files:**
- Modify: `src/infrastructure/plugins/providers/codex.py`
- Test: `tests/test_provider_plugins.py`

- [ ] **Step 1: Write the failing tests for JWT-derived account info, refresh, and availability**

```python
import base64
import json

import pytest

from infrastructure.plugins.providers.codex import CodexPlugin


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8").rstrip("=")
    return f"header.{body}.signature"


@pytest.mark.anyio
async def test_codex_availability_refreshes_near_expiry_token(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = CodexPlugin()
    calls: list[str] = []

    async def fake_refresh(credential: dict[str, str]) -> dict[str, str]:
        calls.append("refresh")
        return {
            **credential,
            "access_token": "fresh-access-token",
            "account_id": "acct_from_refresh",
            "email": "refresh@example.com",
            "expired": "2099-04-09T13:00:00Z",
        }

    class _FakeResponse:
        status_code = 200
        is_success = True

        @staticmethod
        def json() -> dict:
            return {"plan_type": "plus", "rate_limit": {"primary_window": {"used_percent": 12, "reset_at": 1891929600}}}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, *, headers: dict[str, str]) -> _FakeResponse:
            calls.append(headers["Authorization"])
            return _FakeResponse()

    monkeypatch.setattr(plugin, "_refresh_credential", fake_refresh)
    monkeypatch.setattr(
        "infrastructure.plugins.providers.codex.httpx.AsyncClient",
        lambda timeout=10: _FakeClient(),
    )

    credential = {
        "access_token": "stale-access-token",
        "id_token": _jwt(
            {
                "email": "jwt@example.com",
                "sub": "sub-account",
                "https://api.openai.com/auth": {"chatgpt_account_id": "acct-from-jwt"},
            }
        ),
        "refresh_token": "refresh-token",
        "expired": "2000-04-09T13:00:00Z",
    }

    assert await plugin.is_credential_available(credential) is True
    assert calls == ["refresh", "Bearer fresh-access-token"]


@pytest.mark.anyio
async def test_codex_availability_returns_false_on_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = CodexPlugin()

    class _FakeResponse:
        is_success = False
        status_code = 401

        @staticmethod
        def json() -> dict:
            return {"detail": "unauthorized"}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(
        "infrastructure.plugins.providers.codex.httpx.AsyncClient",
        lambda timeout=10: _FakeClient(),
    )

    credential = {
        "access_token": "access-token",
        "id_token": _jwt({"email": "jwt@example.com", "sub": "sub-account"}),
        "refresh_token": "refresh-token",
        "expired": "2099-04-09T13:00:00Z",
    }

    assert await plugin.is_credential_available(credential) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_provider_plugins.py::test_codex_availability_refreshes_near_expiry_token tests/test_provider_plugins.py::test_codex_availability_returns_false_on_auth_failure -v`

Expected: FAIL because the plugin does not yet parse JWTs, refresh credentials, or probe WHAM usage.

- [ ] **Step 3: Implement credential normalization and availability probing**

```python
import base64
import json
from datetime import datetime, timedelta, timezone

import httpx

_AUTH_ORIGIN = "https://auth.openai.com"
_TOKEN_URL = f"{_AUTH_ORIGIN}/oauth/token"
_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

...

    @staticmethod
    def _decode_jwt_payload(id_token: str) -> dict:
        parts = id_token.split(".")
        if len(parts) != 3:
            raise ValueError("invalid id_token")
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8"))

    def _normalize_credential(self, credential: dict[str, str]) -> dict[str, str]:
        normalized = dict(credential)
        claims = self._decode_jwt_payload(normalized["id_token"])
        auth_claims = claims.get("https://api.openai.com/auth") or {}
        normalized["account_id"] = str(
            normalized.get("account_id")
            or auth_claims.get("chatgpt_account_id")
            or claims.get("sub")
            or ""
        )
        normalized["email"] = str(normalized.get("email") or claims.get("email") or "")
        normalized["type"] = "codex"
        return normalized

    @staticmethod
    def _is_near_expiry(credential: dict[str, str], near_minutes: int = 20) -> bool:
        expired = credential.get("expired")
        if not expired:
            return True
        try:
            expiry = datetime.fromisoformat(expired.replace("Z", "+00:00"))
        except ValueError:
            return True
        return expiry <= datetime.now(timezone.utc) + timedelta(minutes=near_minutes)

    async def _refresh_credential(self, credential: dict[str, str]) -> dict[str, str]:
        refresh_token = credential["refresh_token"]
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": _CLIENT_ID,
                    "refresh_token": refresh_token,
                },
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()

        refreshed = self._normalize_credential(
            {
                "id_token": payload["id_token"],
                "access_token": payload["access_token"],
                "refresh_token": payload.get("refresh_token") or refresh_token,
                "expired": (
                    datetime.now(timezone.utc) + timedelta(seconds=int(payload.get("expires_in") or 3600))
                ).isoformat(),
            }
        )
        refreshed["last_refresh"] = datetime.now(timezone.utc).isoformat()
        return refreshed

    async def _prepare_runtime_credential(self, credential: dict[str, str]) -> dict[str, str]:
        normalized = self._normalize_credential(credential)
        if self._is_near_expiry(normalized) and normalized.get("refresh_token"):
            return await self._refresh_credential(normalized)
        return normalized

    async def is_credential_available(self, credential: dict[str, str], model: str | None = None) -> bool:
        runtime_credential = await self._prepare_runtime_credential(credential)
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                _USAGE_URL,
                headers={
                    "Authorization": f"Bearer {runtime_credential['access_token']}",
                    "chatgpt-account-id": runtime_credential["account_id"],
                    "user-agent": "codex_cli_rs/0.111.0 (Windows 10.0.26100; x86_64) WindowsTerminal",
                    "accept": "*/*",
                    "host": "chatgpt.com",
                    "Connection": "close",
                },
            )
        if response.status_code in (401, 403):
            return False
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_provider_plugins.py::test_codex_availability_refreshes_near_expiry_token tests/test_provider_plugins.py::test_codex_availability_returns_false_on_auth_failure -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/infrastructure/plugins/providers/codex.py tests/test_provider_plugins.py
git commit -m "feat: add codex oauth credential refresh and availability checks"
```

### Task 4: Add Capacity Signal And Explain Payload

**Files:**
- Modify: `src/infrastructure/plugins/providers/codex.py`
- Test: `tests/test_provider_plugins.py`

- [ ] **Step 1: Write the failing tests for usage normalization and admin explain output**

```python
import pytest

from infrastructure.plugins.providers.codex import CodexPlugin


@pytest.mark.anyio
async def test_codex_capacity_signal_uses_wham_usage_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = CodexPlugin()

    class _FakeResponse:
        status_code = 200
        is_success = True

        @staticmethod
        def json() -> dict:
            return {
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 25,
                        "reset_at": 1891929600,
                    }
                },
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(plugin, "_prepare_runtime_credential", lambda credential: credential)
    monkeypatch.setattr(
        "infrastructure.plugins.providers.codex.httpx.AsyncClient",
        lambda timeout=10: _FakeClient(),
    )

    signal = await plugin.get_capacity_signal(
        {
            "access_token": "access-token",
            "id_token": "header.payload.signature",
            "refresh_token": "refresh-token",
            "account_id": "acct_123",
            "email": "user@example.com",
            "expired": "2099-04-09T13:00:00Z",
        }
    )

    assert signal is not None
    assert signal.has_capacity_signal is True
    assert signal.capacity_score == pytest.approx(0.75)
    assert signal.quota_available is True
    assert signal.capacity_kind == "remaining_ratio"


@pytest.mark.anyio
async def test_codex_explain_masks_sensitive_fields() -> None:
    plugin = CodexPlugin()

    info = await plugin.explain_credential(
        {
            "access_token": "access-secret-token",
            "id_token": "header.payload.signature",
            "refresh_token": "refresh-secret-token",
            "account_id": "acct_123456",
            "email": "user@example.com",
            "expired": "2099-04-09T13:00:00Z",
        }
    )

    assert info["provider"] == "codex"
    assert info["auth_type"] == "oauth_json"
    assert info["model_source"] == "static"
    assert info["email"] == "user@example.com"
    assert info["account_id"] == "acct_123456"
    assert info["has_refresh_token"] is True
    assert info["access_token_preview"] == "access-s***"
    assert "refresh-secret-token" not in str(info)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_provider_plugins.py::test_codex_capacity_signal_uses_wham_usage_ratio tests/test_provider_plugins.py::test_codex_explain_masks_sensitive_fields -v`

Expected: FAIL because the plugin does not yet convert `used_percent` into a normalized capacity score or return a safe explain payload.

- [ ] **Step 3: Implement capacity normalization and admin explain output**

```python
from infrastructure.plugins.base import CapacitySignal

...

    async def get_capacity_signal(self, credential: dict[str, str]) -> CapacitySignal | None:
        runtime_credential = await self._prepare_runtime_credential(credential)
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                _USAGE_URL,
                headers={
                    "Authorization": f"Bearer {runtime_credential['access_token']}",
                    "chatgpt-account-id": runtime_credential["account_id"],
                    "user-agent": "codex_cli_rs/0.111.0 (Windows 10.0.26100; x86_64) WindowsTerminal",
                    "accept": "*/*",
                    "host": "chatgpt.com",
                    "Connection": "close",
                },
            )
        if response.status_code in (401, 403):
            return None
        if not response.is_success:
            return None

        payload = response.json()
        primary_window = ((payload.get("rate_limit") or {}).get("primary_window") or {})
        used_percent = float(primary_window.get("used_percent") or 0.0)
        remaining = min(max(1.0 - (used_percent / 100.0), 0.0), 1.0)
        return CapacitySignal(
            has_capacity_signal=True,
            capacity_score=remaining,
            quota_available=remaining > 0.0,
            capacity_kind="remaining_ratio",
            reason=f"plan_type={payload.get('plan_type', 'unknown')}",
        )

    async def explain_credential(self, credential: dict[str, str]) -> dict:
        normalized = self._normalize_credential(credential)
        access_token = normalized.get("access_token", "")
        return {
            "provider": self.name,
            "status": "unknown",
            "model_source": self.model_source,
            "auth_type": self.auth_type,
            "email": normalized.get("email", ""),
            "account_id": normalized.get("account_id", ""),
            "expired": normalized.get("expired"),
            "has_refresh_token": bool(normalized.get("refresh_token")),
            "access_token_preview": access_token[:8] + "***" if access_token else "missing",
        }
```

- [ ] **Step 4: Run targeted tests and the provider test file**

Run: `pytest tests/test_provider_plugins.py::test_codex_capacity_signal_uses_wham_usage_ratio tests/test_provider_plugins.py::test_codex_explain_masks_sensitive_fields tests/test_provider_plugins.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/infrastructure/plugins/providers/codex.py tests/test_provider_plugins.py
git commit -m "feat: add codex oauth capacity signal and explain output"
```

### Task 5: Verify The End-to-End Provider Contract

**Files:**
- Modify: `tests/test_provider_plugins.py`
- Modify: `tests/test_container_plugins.py`

- [ ] **Step 1: Add a contract test covering the full plugin surface**

```python
import base64
import json

import pytest

from infrastructure.plugins.providers.codex import CodexPlugin


def _jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8").rstrip("=")
    return f"header.{body}.signature"


@pytest.mark.anyio
async def test_codex_plugin_contract_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = CodexPlugin()

    class _FakeResponse:
        status_code = 200
        is_success = True

        @staticmethod
        def json() -> dict:
            return {
                "plan_type": "plus",
                "rate_limit": {"primary_window": {"used_percent": 40, "reset_at": 1891929600}},
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(
        "infrastructure.plugins.providers.codex.httpx.AsyncClient",
        lambda timeout=10: _FakeClient(),
    )

    credential = {
        "access_token": "access-token",
        "id_token": _jwt(
            {
                "email": "contract@example.com",
                "sub": "sub-account",
                "https://api.openai.com/auth": {"chatgpt_account_id": "acct-contract"},
            }
        ),
        "refresh_token": "refresh-token",
        "expired": "2099-04-09T13:00:00Z",
    }

    assert await plugin.fetch_models(credential)
    assert await plugin.is_credential_available(credential) is True

    signal = await plugin.get_capacity_signal(credential)
    assert signal is not None
    assert signal.capacity_score == pytest.approx(0.6)

    info = await plugin.explain_credential(credential)
    assert info["email"] == "contract@example.com"
    assert info["account_id"] == "acct-contract"
```

- [ ] **Step 2: Run the focused provider and container suites**

Run: `pytest tests/test_provider_plugins.py tests/test_container_plugins.py -v`

Expected: PASS

- [ ] **Step 3: Run the broader plugin-related regression suite**

Run: `pytest tests/test_plugin_registry.py tests/test_provider_plugins.py tests/test_container_plugins.py -v`

Expected: PASS

- [ ] **Step 4: Record any follow-up needed for credential persistence**

```text
Current plugin refreshes credentials in-memory for probes.
If production requires refreshed OAuth bundles to persist back into ApiKey.credential,
add a follow-up design to extend ProviderPlugin with a credential-refresh return path.
This is intentionally out of scope for the first Codex OAuth plugin cut.
```

- [ ] **Step 5: Commit**

```bash
git add tests/test_provider_plugins.py tests/test_container_plugins.py
git commit -m "test: verify codex oauth provider contract"
```

---

## Self-Review

**Spec coverage**
- Credential JSON structure: covered by Task 2 and Task 4.
- Static model list plus `-fast` aliases: covered by Task 2.
- Token refresh via refresh grant: covered by Task 3.
- Usage/quota calculation from `rate_limit.primary_window.used_percent`: covered by Task 4.
- Provider registration and container wiring: covered by Task 1 and Task 5.

**Placeholder scan**
- No `TODO`, `TBD`, or deferred implementation markers remain in executable steps.
- Each code-changing step includes concrete code blocks.
- Each verification step includes exact `pytest` commands and expected outcomes.

**Type consistency**
- Provider class name is consistently `CodexPlugin`.
- Provider identifier is consistently `codex`.
- Credential field names consistently use `access_token`, `id_token`, `refresh_token`, `account_id`, `email`, and `expired`.

Plan complete and saved to `docs/superpowers/plans/2026-04-09-codex-oauth-provider-plugin.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
