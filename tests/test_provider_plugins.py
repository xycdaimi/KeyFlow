import pytest

from infrastructure.plugins.providers.gemini_web_proxy import GeminiWebProxyPlugin, _KNOWN_MODELS


def test_gemini_web_proxy_returns_known_models() -> None:
    plugin = GeminiWebProxyPlugin()
    assert plugin.name == "gemini-web-proxy"
    assert plugin.model_source == "static"
    assert len(_KNOWN_MODELS) > 0


@pytest.mark.anyio
async def test_gemini_web_proxy_fetch_models_returns_static_list() -> None:
    plugin = GeminiWebProxyPlugin()
    models = await plugin.fetch_models("fake-cookie")
    assert models == list(_KNOWN_MODELS)


@pytest.mark.anyio
async def test_gemini_web_proxy_unavailable_without_library() -> None:
    """Without gemini-webapi installed, is_credential_available returns False."""
    import sys
    import unittest.mock as mock

    with mock.patch.dict(sys.modules, {"gemini_webapi": None}):
        plugin = GeminiWebProxyPlugin()
        result = await plugin.is_credential_available("fake-cookie")
        assert result is False


@pytest.mark.anyio
async def test_gemini_web_proxy_explain_includes_dependency_strategy() -> None:
    plugin = GeminiWebProxyPlugin()
    info = await plugin.explain_credential("cookie-value")
    assert info["provider"] == "gemini-web-proxy"
    assert info["model_source"] == "static"
    assert info["dependency"]["name"] == "gemini-webapi"
    assert "degrade_strategy" in info["dependency"]
