"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-29
@Description: Provider 插件契约与行为测试
"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from infrastructure.config.settings import Settings
from infrastructure.plugins.providers.gemini_web_proxy import GeminiWebProxyPlugin, _KNOWN_MODELS
from infrastructure.plugins.providers.gemini import GeminiPlugin
from infrastructure.plugins.providers.gemini_oauth import GeminiOauthPlugin
from infrastructure.plugins.base import CapacitySignal
from infrastructure.plugins.providers.anthropic import AnthropicPlugin
from infrastructure.plugins.providers.antigravity_oauth import AntigravityOauthPlugin
from infrastructure.plugins.providers.codex_oauth import CodexOauthPlugin, _CODEX_VERSION
from infrastructure.plugins.providers.codex_openai import CodexOpenAiPlugin
from infrastructure.plugins.providers.openai import OpenAIPlugin
from infrastructure.plugins.providers.openrouter import OpenRouterPlugin
from tests.fakes import FakeProviderPlugin


def test_provider_base_no_longer_exposes_runtime_bypass_methods() -> None:
    from infrastructure.plugins.base import ProviderPlugin

    assert hasattr(ProviderPlugin, "prepare_credential")
    assert not hasattr(ProviderPlugin, "prepare_runtime_credential")
    assert not hasattr(ProviderPlugin, "is_runtime_credential_available")
    assert not hasattr(ProviderPlugin, "get_runtime_capacity_signal")


@pytest.fixture(autouse=True)
def _default_provider_http_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "infrastructure.plugins.base.get_settings",
        lambda: Settings(_env_file=None),
    )


def test_settings_can_read_provider_proxy_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLOBAL_HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTP_CONNECT_TIMEOUT", "2.5")
    monkeypatch.setenv("HTTP_READ_TIMEOUT", "7.5")
    monkeypatch.setenv("HTTP_TOTAL_TIMEOUT", "11.5")

    settings = Settings(_env_file=None)

    assert settings.global_http_proxy == "http://127.0.0.1:7890"
    assert settings.http_connect_timeout == 2.5
    assert settings.http_read_timeout == 7.5
    assert settings.http_total_timeout == 11.5


def test_openai_build_http_client_uses_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _factory(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(
        "infrastructure.plugins.base.get_settings",
        lambda: Settings(
            _env_file=None,
            GLOBAL_HTTP_PROXY="http://127.0.0.1:7890",
            HTTP_CONNECT_TIMEOUT=2.0,
            HTTP_READ_TIMEOUT=6.0,
            HTTP_TOTAL_TIMEOUT=9.0,
        ),
    )

    plugin = OpenAIPlugin()
    plugin._build_http_client(_factory, total_timeout=15, follow_redirects=True)

    timeout = captured["timeout"]
    assert captured["proxy"] == "http://127.0.0.1:7890"
    assert captured["follow_redirects"] is True
    assert timeout.connect == 2.0
    assert timeout.read == 6.0
    assert timeout.write == 6.0
    assert timeout.pool == 2.0
    assert timeout.connect + timeout.read > 0


def test_openai_build_http_client_uses_config_total_timeout_when_not_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _factory(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(
        "infrastructure.plugins.base.get_settings",
        lambda: Settings(
            _env_file=None,
            GLOBAL_HTTP_PROXY="http://127.0.0.1:7890",
            HTTP_CONNECT_TIMEOUT=2.0,
            HTTP_READ_TIMEOUT=6.0,
            HTTP_TOTAL_TIMEOUT=9.0,
        ),
    )

    plugin = OpenAIPlugin()
    plugin._build_http_client(_factory)

    timeout = captured["timeout"]
    assert timeout.connect == 2.0
    assert timeout.read == 6.0
    assert timeout.write == 6.0
    assert timeout.pool == 2.0
    assert timeout.write + timeout.pool > 0


def test_direct_plugin_build_http_client_does_not_attach_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _factory(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(
        "infrastructure.plugins.base.get_settings",
        lambda: Settings(
            _env_file=None,
            GLOBAL_HTTP_PROXY="http://127.0.0.1:7890",
        ),
    )

    plugin = CodexOpenAiPlugin()
    plugin._build_http_client(_factory, total_timeout=10)

    assert "proxy" not in captured


def test_proxy_plugin_build_http_client_falls_back_to_direct_when_proxy_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _factory(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(
        "infrastructure.plugins.base.get_settings",
        lambda: Settings(_env_file=None, GLOBAL_HTTP_PROXY="   "),
    )

    plugin = OpenAIPlugin()
    plugin._build_http_client(_factory)

    assert "proxy" not in captured


def test_proxy_plugin_build_http_client_uses_proxy_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _factory(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(
        "infrastructure.plugins.base.get_settings",
        lambda: Settings(_env_file=None, GLOBAL_HTTP_PROXY="http://127.0.0.1:7890"),
    )

    plugin = OpenAIPlugin()
    plugin._build_http_client(_factory)

    assert captured["proxy"] == "http://127.0.0.1:7890"


def test_gemini_web_proxy_returns_known_models() -> None:
    plugin = GeminiWebProxyPlugin()
    assert plugin.name == "gemini-web-proxy"
    assert plugin.model_source == "static"
    assert len(_KNOWN_MODELS) > 0


@pytest.mark.anyio
async def test_gemini_web_proxy_fetch_models_returns_static_list() -> None:
    plugin = GeminiWebProxyPlugin()
    models = await plugin.fetch_models(
        {"secure_1psid": "fake-cookie", "secure_1psidts": "fake-cookie-ts"}
    )
    assert models == list(_KNOWN_MODELS)


@pytest.mark.anyio
async def test_gemini_web_proxy_unavailable_without_library() -> None:
    """Without gemini-webapi installed, is_credential_available returns False."""
    import sys
    import unittest.mock as mock

    with mock.patch.dict(sys.modules, {"gemini_webapi": None}):
        plugin = GeminiWebProxyPlugin()
        result = await plugin.is_credential_available(
            {"secure_1psid": "fake-cookie", "secure_1psidts": "fake-cookie-ts"}
        )
        assert result is False


@pytest.mark.anyio
async def test_gemini_web_proxy_explain_includes_dependency_strategy() -> None:
    plugin = GeminiWebProxyPlugin()
    info = await plugin.explain_credential(
        {"secure_1psid": "cookie-value", "secure_1psidts": "cookie-ts-value"}
    )
    assert info["provider"] == "gemini-web-proxy"
    assert info["model_source"] == "static"
    assert info["dependency"]["name"] == "gemini-webapi"
    assert "degrade_strategy" in info["dependency"]


@pytest.mark.anyio
async def test_gemini_web_proxy_requires_both_cookie_fields() -> None:
    plugin = GeminiWebProxyPlugin()

    result = await plugin.is_credential_available({"secure_1psid": "cookie-only"})

    assert result is False


@pytest.mark.anyio
async def test_gemini_web_proxy_uses_both_cookie_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _FakeGeminiClient:
        def __init__(self, *, secure_1psid: str, secure_1psidts: str, proxy: str | None = None):
            captured["secure_1psid"] = secure_1psid
            captured["secure_1psidts"] = secure_1psidts
            captured["proxy"] = proxy

        async def init(self, timeout: int, auto_close: bool, close_delay: int) -> None:
            captured["init_called"] = True

    monkeypatch.setattr(
        "infrastructure.plugins.base.get_settings",
        lambda: Settings(_env_file=None, GLOBAL_HTTP_PROXY="http://127.0.0.1:7890"),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "gemini_webapi",
        type("_FakeGeminiModule", (), {"GeminiClient": _FakeGeminiClient}),
    )

    plugin = GeminiWebProxyPlugin()
    result = await plugin.is_credential_available(
        {"secure_1psid": "cookie-a", "secure_1psidts": "cookie-b"}
    )

    assert result is True
    assert captured["secure_1psid"] == "cookie-a"
    assert captured["secure_1psidts"] == "cookie-b"
    assert captured["proxy"] == "http://127.0.0.1:7890"
    assert captured["init_called"] is True


@pytest.mark.anyio
async def test_gemini_select_probe_model_skips_non_generate_models_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeModels:
        def __init__(self) -> None:
            self.generated_models: list[str] = []

        async def list(self, *args, **kwargs):
            async def _items():
                yield {
                    "name": "models/imagen-3.0-generate",
                    "supportedGenerationMethods": ["predict"],
                }
                yield {
                    "name": "models/gemini-2.0-flash",
                    "supportedGenerationMethods": ["generateContent"],
                }

            return _items()

        async def generate_content(self, *args, **kwargs) -> dict:
            self.generated_models.append(kwargs["model"])
            return {"candidates": []}

    class _FakeAioClient:
        def __init__(self, models: _FakeModels) -> None:
            self.models = models
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class _FakeClient:
        def __init__(self, **kwargs) -> None:
            self.aio = _FakeAioClient(fake_models)

    fake_models = _FakeModels()
    monkeypatch.setattr("infrastructure.plugins.providers.gemini.genai.Client", _FakeClient)
    plugin = GeminiPlugin()

    assert await plugin.is_credential_available({"api_key": "AIza-test"}) is True
    assert fake_models.generated_models == ["gemini-2.0-flash"]


@pytest.mark.anyio
async def test_gemini_vertex_ai_fetch_models_uses_genai_client_with_vertex_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeClient:
        def __init__(self, **kwargs) -> None:
            raise AssertionError("Vertex AI fetch_models must not call genai.Client")

    monkeypatch.setattr("infrastructure.plugins.providers.gemini.genai.Client", _FakeClient)

    plugin = GeminiPlugin()
    models = await plugin.fetch_models(
        {
            "api_key": "AQ.A-test",
            "vertexai": "true",
        }
    )

    assert "gemini-2.5-flash" in models
    assert "gemini-2.5-pro" in models
    assert "gemini-3.1-flash-image" in models
    assert "gemini-claude-sonnet-4-6" in models


@pytest.mark.anyio
async def test_gemini_vertex_ai_availability_generates_with_sdk_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeModels:
        def __init__(self) -> None:
            self.generated_models: list[str] = []

        async def list(self, *args, **kwargs):
            raise AssertionError("Vertex AI availability must not call models.list")

        async def generate_content(self, *args, **kwargs) -> dict:
            self.generated_models.append(kwargs["model"])
            captured["contents"] = kwargs["contents"]
            captured["config"] = kwargs["config"]
            return {"candidates": []}

    class _FakeAioClient:
        def __init__(self, models: _FakeModels) -> None:
            self.models = models

        async def aclose(self) -> None:
            return None

    class _FakeClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)
            self.aio = _FakeAioClient(fake_models)

    fake_models = _FakeModels()
    monkeypatch.setattr("infrastructure.plugins.providers.gemini.genai.Client", _FakeClient)
    plugin = GeminiPlugin()
    available = await plugin.is_credential_available(
        {
            "api_key": "AQ.A-test",
            "vertexai": "true",
        }
    )

    assert available is True
    assert captured["vertexai"] is True
    assert "project" not in captured
    assert "location" not in captured
    assert fake_models.generated_models == ["gemini-2.5-flash"]
    assert captured["contents"] == "ping"


@pytest.mark.anyio
async def test_openrouter_explain_returns_redacted_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeResponse:
        is_success = True

        @staticmethod
        def json() -> dict:
            return {
                "data": {
                    "is_free_tier": False,
                    "limit": 300,
                    "limit_reset": "monthly",
                    "limit_remaining": 94.72626641,
                    "usage": 525.41113258,
                    "usage_monthly": 205.27373359,
                    "include_byok_in_limit": False,
                    "label": "test-key",
                }
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(
        "infrastructure.plugins.providers.openrouter.httpx.AsyncClient",
        lambda timeout=10: _FakeClient(),
    )

    plugin = OpenRouterPlugin()
    info = await plugin.explain_credential({"api_key": "sk-or-secret"})

    assert info["provider"] == "openrouter"
    assert info["auth_type"] == "bearer_api_key"
    assert info["model_source"] == "remote"
    assert info["remaining_usd"] == 94.7263
    assert info["credential_hint"] == "sk-or-se***"
    assert "sk-or-secret" not in str(info)


@pytest.mark.anyio
async def test_openrouter_explain_uses_limit_reset_period_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeResponse:
        is_success = True

        @staticmethod
        def json() -> dict:
            return {
                "data": {
                    "is_free_tier": False,
                    "limit": 300,
                    "limit_reset": "monthly",
                    "usage": 525.41113258,
                    "usage_daily": 2.5930446,
                    "usage_weekly": 34.83387524,
                    "usage_monthly": 205.27373359,
                    "include_byok_in_limit": False,
                    "label": "period-key",
                }
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(
        "infrastructure.plugins.providers.openrouter.httpx.AsyncClient",
        lambda timeout=10: _FakeClient(),
    )

    plugin = OpenRouterPlugin()
    info = await plugin.explain_credential({"api_key": "sk-or-secret"})

    assert info["remaining_usd"] == 94.7263


@pytest.mark.anyio
async def test_openrouter_availability_uses_limit_reset_period_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeResponse:
        status_code = 200
        is_success = True

        @staticmethod
        def json() -> dict:
            return {
                "data": {
                    "is_free_tier": False,
                    "limit": 300,
                    "limit_reset": "monthly",
                    "usage": 525.41113258,
                    "usage_daily": 2.5930446,
                    "usage_weekly": 34.83387524,
                    "usage_monthly": 205.27373359,
                }
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(
        "infrastructure.plugins.providers.openrouter.httpx.AsyncClient",
        lambda timeout=10: _FakeClient(),
    )

    plugin = OpenRouterPlugin()

    assert await plugin.is_credential_available({"api_key": "sk-or-secret"}) is True


@pytest.mark.anyio
async def test_openrouter_availability_remains_true_when_period_budget_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        status_code = 200
        is_success = True

        @staticmethod
        def json() -> dict:
            return {
                "data": {
                    "is_free_tier": False,
                    "limit": 300,
                    "limit_reset": "monthly",
                    "usage": 100.0,
                    "usage_monthly": 301.0,
                }
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(
        "infrastructure.plugins.providers.openrouter.httpx.AsyncClient",
        lambda timeout=10: _FakeClient(),
    )

    plugin = OpenRouterPlugin()

    assert await plugin.is_credential_available({"api_key": "sk-or-secret"}) is True


@pytest.mark.anyio
async def test_openrouter_capacity_marks_quota_unavailable_when_budget_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        is_success = True

        @staticmethod
        def json() -> dict:
            return {
                "data": {
                    "is_free_tier": False,
                    "limit": 300,
                    "limit_reset": "monthly",
                    "usage_monthly": 301.0,
                }
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(
        "infrastructure.plugins.providers.openrouter.httpx.AsyncClient",
        lambda timeout=10: _FakeClient(),
    )

    plugin = OpenRouterPlugin()
    signal = await plugin.get_capacity_signal({"api_key": "sk-or-secret"})

    assert signal is not None
    assert signal.quota_available is False


@pytest.mark.anyio
async def test_openai_availability_uses_models_endpoint_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class _FakeResponse:
        status_code = 200
        is_success = True

        @staticmethod
        def json() -> dict:
            return {"data": [{"id": "gpt-4.1"}]}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, *args, **kwargs) -> _FakeResponse:
            calls.append(url)
            return _FakeResponse()

    monkeypatch.setattr(
        "infrastructure.plugins.providers.openai.httpx.AsyncClient",
        lambda timeout=10: _FakeClient(),
    )

    plugin = OpenAIPlugin()

    assert await plugin.is_credential_available({"api_key": "sk-test"}) is True
    assert calls == ["https://api.openai.com/v1/models"]


@pytest.mark.anyio
async def test_openai_availability_returns_false_for_auth_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        is_success = False

        def __init__(self, status_code: int):
            self.status_code = status_code

        @staticmethod
        def json() -> dict:
            return {"error": {"message": "You exceeded your current quota"}}

    class _FakeClient:
        def __init__(self, status_code: int):
            self._status_code = status_code

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse(self._status_code)

    for status_code in (401, 403):
        monkeypatch.setattr(
            "infrastructure.plugins.providers.openai.httpx.AsyncClient",
            lambda timeout=10, status_code=status_code: _FakeClient(status_code),
        )
        plugin = OpenAIPlugin()
        assert await plugin.is_credential_available({"api_key": "sk-test"}) is False


@pytest.mark.anyio
async def test_openai_availability_keeps_true_for_quota_429(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeResponse:
        is_success = False
        status_code = 429

        @staticmethod
        def json() -> dict:
            return {"error": {"message": "You exceeded your current quota"}}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(
        "infrastructure.plugins.providers.openai.httpx.AsyncClient",
        lambda timeout=10: _FakeClient(),
    )

    plugin = OpenAIPlugin()
    assert await plugin.is_credential_available({"api_key": "sk-test"}) is True

    signal = await plugin.get_capacity_signal({"api_key": "sk-test"})
    assert signal is not None
    assert signal.quota_available is False


@pytest.mark.anyio
async def test_openai_availability_keeps_available_on_transient_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        status_code = 500
        is_success = False

        @staticmethod
        def json() -> dict:
            return {"error": {"message": "server error"}}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(
        "infrastructure.plugins.providers.openai.httpx.AsyncClient",
        lambda timeout=10: _FakeClient(),
    )

    plugin = OpenAIPlugin()

    assert await plugin.is_credential_available({"api_key": "sk-test"}) is True


@pytest.mark.anyio
async def test_gemini_oauth_prepare_credential_preserves_user_project_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 4, 21, 8, 0, 0, tzinfo=timezone.utc)

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
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, *args, **kwargs) -> _FakeResponse:
            raise AssertionError("prepare_credential must not do remote IO")

    monkeypatch.setattr(
        "infrastructure.plugins.providers.gemini_oauth.httpx.AsyncClient",
        lambda **kwargs: _FakeClient(),
    )

    plugin = GeminiOauthPlugin()
    result = await plugin.prepare_credential(
        {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "project_id": "project-123",
            "expiry_date": "9999999999999",
        }
    )

    assert result.changed is True
    assert result.credential["project_id"] == "project-123"


@pytest.mark.anyio
async def test_gemini_oauth_refresh_oauth_credential_returns_persistable_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 4, 21, 8, 0, 0, tzinfo=timezone.utc)

    class _FakeResponse:
        status_code = 200
        is_success = True

        def raise_for_status(self) -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {
                "access_token": "new-access",
                "token_type": "Bearer",
                "scope": "https://www.googleapis.com/auth/cloud-platform",
                "expires_in": 3600,
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *args, **kwargs) -> _FakeResponse:
            if url == "https://oauth2.googleapis.com/token":
                return _FakeResponse()
            raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("infrastructure.plugins.providers.gemini_oauth._utc_now", lambda: fixed_now)
    monkeypatch.setattr(
        "infrastructure.plugins.providers.gemini_oauth.httpx.AsyncClient",
        lambda **kwargs: _FakeClient(),
    )

    plugin = GeminiOauthPlugin()
    refreshed = await plugin._refresh_oauth_credential(
        {
            "access_token": "old-access",
            "refresh_token": "refresh-token",
            "project_id": "project-123",
            "expiry_date": str(int((fixed_now - timedelta(minutes=5)).timestamp() * 1000)),
        }
    )

    assert refreshed is not None
    assert refreshed["access_token"] == "new-access"
    assert refreshed["token_type"] == "Bearer"
    assert refreshed["last_refresh"] == "2026-04-21T08:00:00+00:00"
    assert refreshed["project_id"] == "project-123"


@pytest.mark.anyio
async def test_gemini_oauth_refresh_oauth_credential_returns_none_when_refresh_response_missing_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 4, 21, 8, 0, 0, tzinfo=timezone.utc)

    class _FakeResponse:
        status_code = 200
        is_success = True

        def raise_for_status(self) -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {
                "token_type": "Bearer",
                "scope": "https://www.googleapis.com/auth/cloud-platform",
                "expires_in": 3600,
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *args, **kwargs) -> _FakeResponse:
            if url == "https://oauth2.googleapis.com/token":
                return _FakeResponse()
            raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("infrastructure.plugins.providers.gemini_oauth._utc_now", lambda: fixed_now)
    monkeypatch.setattr(
        "infrastructure.plugins.providers.gemini_oauth.httpx.AsyncClient",
        lambda **kwargs: _FakeClient(),
    )

    plugin = GeminiOauthPlugin()
    refreshed = await plugin._refresh_oauth_credential(
        {
            "refresh_token": "refresh-token",
            "expiry_date": str(int((fixed_now - timedelta(minutes=5)).timestamp() * 1000)),
        }
    )

    assert refreshed is None


@pytest.mark.anyio
async def test_gemini_oauth_fetch_models_uses_quota_models_with_remaining_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        status_code = 200
        is_success = True

        @staticmethod
        def json() -> dict:
            return {
                "buckets": [
                    {"modelId": "gemini-2.5-pro", "remainingFraction": 0.8},
                    {"modelId": "gemini-2.5-flash", "remainingFraction": 0.25},
                    {"modelId": "gemini-2.5-flash-lite", "remainingFraction": 0.0},
                    {"modelId": "new-quota-model", "remainingFraction": 1.0},
                ]
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(
        "infrastructure.plugins.providers.gemini_oauth.httpx.AsyncClient",
        lambda **kwargs: _FakeClient(),
    )

    plugin = GeminiOauthPlugin()
    models = await plugin.fetch_models(
        {
            "access_token": "access",
            "refresh_token": "refresh",
            "project_id": "project-123",
            "expiry_date": "9999999999999",
        }
    )

    assert models == [
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "new-quota-model",
    ]


@pytest.mark.anyio
async def test_gemini_oauth_fetch_models_falls_back_to_fixed_models_only_when_quota_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        def __init__(self, status_code: int, payload: dict):
            self.status_code = status_code
            self.is_success = 200 <= status_code < 300
            self._payload = payload

        def raise_for_status(self) -> None:
            if not self.is_success:
                raise RuntimeError("http error")

        def json(self) -> dict:
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
    runtime_credential = {
        "access_token": "access",
        "refresh_token": "refresh",
        "project_id": "project-123",
        "expiry_date": "9999999999999",
    }
    models = await plugin.fetch_models(runtime_credential)

    assert models == plugin._fallback_models()


@pytest.mark.anyio
async def test_gemini_oauth_fetch_models_without_runtime_project_id_uses_fallback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *args, **kwargs):
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


@pytest.mark.anyio
async def test_gemini_oauth_capacity_uses_best_remaining_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        status_code = 200
        is_success = True

        @staticmethod
        def json() -> dict:
            return {
                "buckets": [
                    {"modelId": "gemini-2.5-flash", "remainingFraction": 0.25, "resetTime": "2026-04-22T00:00:00Z"},
                    {"modelId": "gemini-2.5-pro", "remainingFraction": 0.8, "resetTime": "2026-04-22T00:00:00Z"},
                ]
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(
        "infrastructure.plugins.providers.gemini_oauth.httpx.AsyncClient",
        lambda **kwargs: _FakeClient(),
    )

    plugin = GeminiOauthPlugin()
    signal = await plugin.get_capacity_signal(
        {
            "access_token": "access",
            "refresh_token": "refresh",
            "project_id": "project-123",
            "expiry_date": "9999999999999",
        }
    )

    assert signal is not None
    assert signal.capacity_kind == "remaining_ratio"
    assert signal.capacity_score == pytest.approx(0.8)
    assert signal.quota_available is True


@pytest.mark.anyio
async def test_gemini_oauth_capacity_marks_exhausted_when_supported_buckets_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        status_code = 200
        is_success = True

        @staticmethod
        def json() -> dict:
            return {
                "buckets": [
                    {"modelId": "gemini-2.5-flash", "remainingFraction": 0.0},
                    {"modelId": "gemini-2.5-pro", "remainingFraction": 0.0},
                ]
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(
        "infrastructure.plugins.providers.gemini_oauth.httpx.AsyncClient",
        lambda **kwargs: _FakeClient(),
    )

    plugin = GeminiOauthPlugin()
    credential = {
        "access_token": "access",
        "refresh_token": "refresh",
        "project_id": "project-123",
        "expiry_date": "9999999999999",
    }

    assert await plugin.is_credential_available(credential) is True
    signal = await plugin.get_capacity_signal(credential)

    assert signal is not None
    assert signal.capacity_score == 0.0
    assert signal.quota_available is False


@pytest.mark.anyio
async def test_gemini_oauth_availability_falls_back_to_generation_when_quota_fails(
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
            self.calls: list[tuple[str, dict | None, dict | None]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *args, **kwargs) -> _FakeResponse:
            self.calls.append((url, kwargs.get("json"), kwargs.get("headers")))
            if url.endswith(":retrieveUserQuota"):
                return _FakeResponse({"error": {"message": "forbidden"}}, status_code=403)
            if url.endswith(":loadCodeAssist"):
                return _FakeResponse({"cloudaicompanionProject": "project-123"})
            if url.endswith(":generateContent"):
                return _FakeResponse({"response": {"candidates": []}})
            raise AssertionError(f"unexpected url: {url}")

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "infrastructure.plugins.providers.gemini_oauth.httpx.AsyncClient",
        lambda **kwargs: fake_client,
    )

    plugin = GeminiOauthPlugin()
    available = await plugin.is_credential_available(
        {
            "access_token": "access",
            "refresh_token": "refresh",
            "project_id": "project-123",
            "expiry_date": "9999999999999",
        }
    )
    signal = await plugin.get_capacity_signal(
        {
            "access_token": "access",
            "refresh_token": "refresh",
            "project_id": "project-123",
            "expiry_date": "9999999999999",
        }
    )

    assert available is True
    assert signal is None
    assert [call[0] for call in fake_client.calls] == [
        "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
        "https://cloudcode-pa.googleapis.com/v1internal:generateContent",
        "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
    ]
    assert fake_client.calls[1][1]["model"] == plugin._fallback_models()[0]
    assert fake_client.calls[1][1]["project"] == "project-123"
    assert fake_client.calls[0][2] == {
        "Content-Type": "application/json",
        "Authorization": "Bearer access",
    }
    assert "User-Agent" not in fake_client.calls[0][2]
    assert "X-Goog-Api-Client" not in fake_client.calls[0][2]


@pytest.mark.anyio
async def test_gemini_oauth_availability_requires_runtime_project_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *args, **kwargs):
            raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(
        "infrastructure.plugins.providers.gemini_oauth.httpx.AsyncClient",
        lambda **kwargs: _FakeClient(),
    )

    plugin = GeminiOauthPlugin()

    assert (
        await plugin.is_credential_available(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "expiry_date": "9999999999999",
            }
        )
        is False
    )


@pytest.mark.anyio
async def test_gemini_oauth_project_discovery_requests_do_not_send_user_agent(
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
            self.calls: list[tuple[str, dict | None, dict | None]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, *args, **kwargs) -> _FakeResponse:
            self.calls.append((url, kwargs.get("json"), kwargs.get("headers")))
            if url.endswith(":loadCodeAssist"):
                return _FakeResponse({"allowedTiers": [{"id": "free-tier", "isDefault": True}]})
            if url.endswith(":onboardUser"):
                return _FakeResponse({"done": True, "response": {"cloudaicompanionProject": {"id": "project-123"}}})
            raise AssertionError(f"unexpected url: {url}")

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "infrastructure.plugins.providers.gemini_oauth.httpx.AsyncClient",
        lambda **kwargs: fake_client,
    )

    plugin = GeminiOauthPlugin()
    runtime_credential = await plugin._build_runtime_credential(
        {
            "access_token": "access",
            "refresh_token": "refresh",
            "expiry_date": "9999999999999",
        }
    )

    assert runtime_credential["project_id"] == "project-123"
    assert [call[0] for call in fake_client.calls] == [
        "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
        "https://cloudcode-pa.googleapis.com/v1internal:onboardUser",
    ]
    assert "User-Agent" not in fake_client.calls[0][2]
    assert "User-Agent" not in fake_client.calls[1][2]
    assert fake_client.calls[0][2]["X-Goog-Api-Client"] == "google-genai-sdk/1.41.0 gl-node/v22.19.0"
    assert fake_client.calls[1][2]["X-Goog-Api-Client"] == "google-genai-sdk/1.41.0 gl-node/v22.19.0"


def test_gemini_oauth_prepare_credential_requires_access_token_or_refresh_token() -> None:
    plugin = GeminiOauthPlugin()

    with pytest.raises(ValueError, match="credential.refresh_token or credential.access_token is required"):
        plugin._normalize_credential({})


def test_gemini_oauth_prepare_credential_accepts_refresh_token_only() -> None:
    plugin = GeminiOauthPlugin()

    normalized = plugin._normalize_credential({"refresh_token": "refresh-token"})

    assert normalized["refresh_token"] == "refresh-token"
    assert normalized["type"] == "gemini_cli_oauth"


def test_gemini_oauth_freshness_requires_refresh_when_access_token_missing_but_refresh_exists() -> None:
    plugin = GeminiOauthPlugin()

    assert plugin._is_oauth_credential_fresh({"refresh_token": "refresh-token"}) is False


def test_gemini_oauth_freshness_allows_access_token_only_when_refresh_token_missing() -> None:
    plugin = GeminiOauthPlugin()

    assert plugin._is_oauth_credential_fresh({"access_token": "access-token"}) is True


@pytest.mark.anyio
async def test_gemini_oauth_prepare_and_explain_preserve_email() -> None:
    plugin = GeminiOauthPlugin()

    result = await plugin.prepare_credential(
        {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "email": " user@example.com ",
        }
    )

    assert result.credential["email"] == "user@example.com"
    info = await plugin.explain_credential(result.credential)
    assert info["email"] == "user@example.com"


@pytest.mark.anyio
async def test_antigravity_oauth_prepare_and_explain_preserve_email() -> None:
    plugin = AntigravityOauthPlugin()

    result = await plugin.prepare_credential(
        {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "email": " user@example.com ",
        }
    )

    assert result.credential["email"] == "user@example.com"
    info = await plugin.explain_credential(result.credential)
    assert info["email"] == "user@example.com"


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

    monkeypatch.setattr("infrastructure.plugins.providers.codex_oauth._utc_now", lambda: fixed_now)
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
    assert refreshed["account_id"] == "new-account"
    assert refreshed["email"] == "new@example.com"
    assert refreshed["last_refresh"] == "2026-04-14T08:00:00+00:00"


@pytest.mark.anyio
async def test_codex_oauth_refresh_oauth_credential_returns_none_on_refresh_failure(
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
    id_token = _jwt(
        {
            "email": "old@example.com",
            "sub": "old-sub",
            "https://api.openai.com/auth": {"chatgpt_account_id": "old-account"},
        }
    )

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, *args, **kwargs):
            raise httpx.HTTPError("refresh failed")

    monkeypatch.setattr("infrastructure.plugins.providers.codex_oauth._utc_now", lambda: fixed_now)
    monkeypatch.setattr(
        "infrastructure.plugins.providers.codex_oauth.httpx.AsyncClient",
        lambda **kwargs: _FakeClient(),
    )

    plugin = CodexOauthPlugin()
    refreshed = await plugin._refresh_oauth_credential(
        {
            "access_token": "old-access",
            "id_token": id_token,
            "refresh_token": "old-refresh",
            "expired": "2026-04-14T08:04:00Z",
        }
    )

    assert refreshed is None


@pytest.mark.anyio
async def test_codex_oauth_fetch_models_matches_aiclient2api_baseline() -> None:
    plugin = CodexOauthPlugin()

    models = await plugin.fetch_models(
        {
            "access_token": "access-token",
            "id_token": "header.payload.signature",
        }
    )

    assert models == [
        "gpt-5.2",
        "gpt-5.3-codex",
        "gpt-5.3-codex-spark",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.2-fast",
        "gpt-5.3-codex-fast",
        "gpt-5.3-codex-spark-fast",
        "gpt-5.4-fast",
        "gpt-5.4-mini-fast",
    ]


@pytest.mark.anyio
async def test_codex_oauth_prepare_credential_accepts_precomputed_account_id() -> None:
    plugin = CodexOauthPlugin()

    result = await plugin.prepare_credential(
        {
            "access_token": "access-token",
            "account_id": "account-id",
            "email": "user@example.com",
        }
    )

    assert result.credential["access_token"] == "access-token"
    assert result.credential["account_id"] == "account-id"
    assert result.credential["email"] == "user@example.com"
    assert result.credential["type"] == "codex"


def test_codex_oauth_usage_headers_use_updated_codex_version() -> None:
    headers = CodexOauthPlugin._usage_headers(
        {
            "access_token": "access-token",
            "account_id": "account-id",
        }
    )

    assert headers["user-agent"] == (
        f"codex-tui/{_CODEX_VERSION} "
        f"(Windows 10.0.26100; x86_64) WindowsTerminal "
        f"(codex-tui; {_CODEX_VERSION})"
    )
    assert _CODEX_VERSION == "0.118.0"


@pytest.mark.anyio
async def test_codex_oauth_availability_uses_usage_without_probe_when_capacity_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        def __init__(self, payload: dict, status_code: int = 200):
            self._payload = payload
            self.status_code = status_code
            self.is_success = 200 <= status_code < 300

        def json(self) -> dict:
            return self._payload

    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict | None]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, *args, **kwargs) -> _FakeResponse:
            self.calls.append(("GET", url, None))
            return _FakeResponse({"plan_type": "plus", "rate_limit": {"primary_window": {"used_percent": 25}}})

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "infrastructure.plugins.providers.codex_oauth.httpx.AsyncClient",
        lambda **kwargs: fake_client,
    )

    plugin = CodexOauthPlugin()
    available = await plugin.is_credential_available(
        {
            "access_token": "access-token",
            "account_id": "account-id",
        }
    )

    assert available is True
    assert fake_client.calls == [("GET", "https://chatgpt.com/backend-api/wham/usage", None)]


@pytest.mark.anyio
async def test_codex_oauth_availability_keeps_true_when_usage_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        def __init__(self, payload: dict, status_code: int = 200):
            self._payload = payload
            self.status_code = status_code
            self.is_success = 200 <= status_code < 300

        def json(self) -> dict:
            return self._payload

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse({"plan_type": "plus", "rate_limit": {"primary_window": {"used_percent": 100}}})

    monkeypatch.setattr(
        "infrastructure.plugins.providers.codex_oauth.httpx.AsyncClient",
        lambda **kwargs: _FakeClient(),
    )

    plugin = CodexOauthPlugin()

    assert await plugin.is_credential_available(
        {
            "access_token": "access-token",
            "account_id": "account-id",
        }
    ) is True


@pytest.mark.anyio
async def test_codex_oauth_availability_uses_wham_usage_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        def __init__(self, payload: dict, status_code: int = 200):
            self._payload = payload
            self.status_code = status_code
            self.is_success = 200 <= status_code < 300

        def json(self) -> dict:
            return self._payload

    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict | None, dict | None]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, *args, **kwargs) -> _FakeResponse:
            self.calls.append(("GET", url, None, kwargs.get("headers")))
            return _FakeResponse({"error": "forbidden"}, status_code=403)

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "infrastructure.plugins.providers.codex_oauth.httpx.AsyncClient",
        lambda **kwargs: fake_client,
    )

    plugin = CodexOauthPlugin()
    available = await plugin.is_credential_available(
        {
            "access_token": "access-token",
            "account_id": "account-id",
        }
    )

    assert available is False
    assert fake_client.calls == [
        (
                "GET",
                "https://chatgpt.com/backend-api/wham/usage",
                None,
                {
                    "user-agent": (
                        f"codex-tui/{_CODEX_VERSION} "
                        f"(Windows 10.0.26100; x86_64) WindowsTerminal "
                        f"(codex-tui; {_CODEX_VERSION})"
                    ),
                    "authorization": "Bearer access-token",
                    "chatgpt-account-id": "account-id",
                    "accept": "*/*",
                "host": "chatgpt.com",
                "Connection": "close",
            },
        )
    ]


@pytest.mark.anyio
async def test_codex_oauth_availability_keeps_true_when_usage_payload_is_not_decisive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        def __init__(self, payload: dict, status_code: int = 200):
            self._payload = payload
            self.status_code = status_code
            self.is_success = 200 <= status_code < 300

        def json(self) -> dict:
            return self._payload

    class _FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, *args, **kwargs) -> _FakeResponse:
            self.calls.append(url)
            return _FakeResponse({"plan_type": "plus"})

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "infrastructure.plugins.providers.codex_oauth.httpx.AsyncClient",
        lambda **kwargs: fake_client,
    )

    plugin = CodexOauthPlugin()

    assert await plugin.is_credential_available(
        {
            "access_token": "access-token",
            "account_id": "account-id",
        }
    ) is True
    assert fake_client.calls == ["https://chatgpt.com/backend-api/wham/usage"]


@pytest.mark.anyio
async def test_codex_oauth_capacity_signal_returns_none_when_usage_payload_is_not_decisive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        status_code = 200
        is_success = True

        @staticmethod
        def json() -> dict:
            return {"plan_type": "plus"}

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

    assert await plugin.get_capacity_signal(
        {
            "access_token": "access-token",
            "account_id": "account-id",
        }
    ) is None


@pytest.mark.anyio
async def test_fake_provider_can_return_capacity_signal() -> None:
    plugin = FakeProviderPlugin(
        "openrouter",
        available=True,
        capacity_signal=CapacitySignal(
            has_capacity_signal=True,
            capacity_score=0.75,
            capacity_kind="remaining_budget_ratio",
            reason="remaining budget available",
        ),
    )

    signal = await plugin.get_capacity_signal({"api_key": "sk-test"})

    assert signal is not None
    assert signal.capacity_score == 0.75


@pytest.mark.anyio
async def test_openrouter_capacity_signal_returns_normalized_budget_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        is_success = True

        @staticmethod
        def json() -> dict:
            return {
                "data": {
                    "is_free_tier": False,
                    "limit": 300,
                    "limit_reset": "monthly",
                    "usage_monthly": 205.27373359,
                    "label": "signal-key",
                }
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, *args, **kwargs) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(
        "infrastructure.plugins.providers.openrouter.httpx.AsyncClient",
        lambda timeout=10: _FakeClient(),
    )

    plugin = OpenRouterPlugin()
    signal = await plugin.get_capacity_signal({"api_key": "sk-or-secret"})

    assert signal is not None
    assert signal.has_capacity_signal is True
    assert signal.capacity_kind == "remaining_budget_ratio"
    assert signal.capacity_score == pytest.approx(94.72626641 / 300, rel=1e-4)


@pytest.mark.anyio
async def test_providers_without_reliable_quota_data_return_no_capacity_signal() -> None:
    providers = [
        OpenAIPlugin(),
        AnthropicPlugin(),
        GeminiPlugin(),
        GeminiWebProxyPlugin(),
    ]

    for plugin in providers:
        signal = await plugin.get_capacity_signal({"api_key": "fake"})
        assert signal is None
