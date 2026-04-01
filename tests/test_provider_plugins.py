import pytest

from infrastructure.plugins.providers.gemini_web_proxy import GeminiWebProxyPlugin, _KNOWN_MODELS
from infrastructure.plugins.providers.gemini import GeminiPlugin
from infrastructure.plugins.base import CapacitySignal
from infrastructure.plugins.providers.anthropic import AnthropicPlugin
from infrastructure.plugins.providers.openai import OpenAIPlugin
from infrastructure.plugins.providers.openrouter import OpenRouterPlugin
from tests.fakes import FakeProviderPlugin


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
        def __init__(self, *, secure_1psid: str, secure_1psidts: str):
            captured["secure_1psid"] = secure_1psid
            captured["secure_1psidts"] = secure_1psidts

        async def init(self, timeout: int, auto_close: bool, close_delay: int) -> None:
            captured["init_called"] = True

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
    assert captured["init_called"] is True


@pytest.mark.anyio
async def test_gemini_select_probe_model_skips_non_generate_models_across_pages(
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
            self.get_calls = 0
            self.post_urls: list[str] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, *args, **kwargs) -> _FakeResponse:
            self.get_calls += 1
            if self.get_calls == 1:
                return _FakeResponse(
                    {
                        "models": [
                            {
                                "name": "models/imagen-3.0-generate",
                                "supportedGenerationMethods": ["predict"],
                            }
                        ],
                        "nextPageToken": "page-2",
                    }
                )
            return _FakeResponse(
                {
                    "models": [
                        {
                            "name": "models/gemini-2.0-flash",
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ]
                }
            )

        async def post(self, *args, **kwargs) -> _FakeResponse:
            self.post_urls.append(args[0])
            return _FakeResponse({"candidates": []})

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "infrastructure.plugins.providers.gemini.httpx.AsyncClient",
        lambda timeout=10: fake_client,
    )

    plugin = GeminiPlugin()

    assert await plugin.is_credential_available({"api_key": "AIza-test"}) is True
    assert fake_client.post_urls == [
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
    ]


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
