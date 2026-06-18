"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-06-17
@Description: Google Gemini official API provider plugin
"""
from __future__ import annotations

from typing import Any

import httpx
from google import genai
from google.genai import types

from infrastructure.config.settings import get_settings
from infrastructure.plugins.base import EgressMode, ProviderPlugin

_BASE_URL = "https://generativelanguage.googleapis.com"
_VERTEXAI_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_VERTEXAI_MODELS: tuple[str, ...] = (
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
    "gemini-2.5-pro-preview-06-05",
    "gemini-2.5-flash-preview-09-2025",
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash-image",
    "gemini-3.1-flash-image",
)
_VERTEXAI_PROBE_MODEL = "gemini-2.5-flash"


class GeminiCustomPlugin(ProviderPlugin):
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_INTERFACE_VERSION = "1.0.0"

    @property
    def name(self) -> str:
        return "gemini_custom"

    @property
    def description(self) -> str:
        return "Google Gemini official API for AI Studio and Vertex AI API keys."

    @property
    def auth_type(self) -> str:
        return "header_api_key"

    @property
    def credential_hint(self) -> str:
        return (
            '{"api_key": "AIza...", "base_url": "https://example.com"} '
            "(Google AI Studio API Key) or "
            '{"api_key": "AQ.A...", "vertexai": true, "base_url": "https://example.com"} '
            "(Vertex AI API Key)"
        )

    @property
    def egress_mode(self) -> EgressMode:
        return "proxy"

    def is_plugin_ready(self) -> bool:
        return True

    @staticmethod
    def _api_key(credential: dict[str, Any]) -> str:
        return str(credential["api_key"])

    @staticmethod
    def _is_vertexai(credential: dict[str, Any]) -> bool:
        if credential.get("vertexai") is True:
            return True
        return str(credential.get("vertexai", "")).strip().lower() in _VERTEXAI_TRUE_VALUES

    @staticmethod
    def _base_url(credential: dict[str, Any]) -> str:
        return str(credential.get("base_url") or credential.get("endpoint") or _BASE_URL).strip()

    @staticmethod
    def _normalize_sdk_model_name(model: str) -> str:
        marker = "/publishers/google/models/"
        if marker in model:
            return model.rsplit(marker, 1)[1]
        for prefix in ("publishers/google/models/", "models/"):
            if model.startswith(prefix):
                return model.removeprefix(prefix)
        return model

    async def verify_upstream_root_reachable(self) -> None:
        await self._ensure_upstream_root_http_reachable(_BASE_URL, httpx.AsyncClient)

    def _build_genai_client(self, credential: dict[str, Any]):
        api_key = self._api_key(credential)
        vertexai = self._is_vertexai(credential)
        base_url = self._base_url(credential)

        client_kwargs = {"api_key": api_key}
        http_options = self._build_genai_http_options(base_url)
        if http_options is not None:
            client_kwargs["http_options"] = http_options
        if vertexai:
            client_kwargs["vertexai"] = vertexai

        return genai.Client(**client_kwargs)

    def _build_genai_http_options(self, endpoint: str):
        proxy = self._proxy_url()
        settings = get_settings()
        timeout_ms = int(max(settings.http_total_timeout, 0.1) * 1000)
        http_options_kwargs = {"apiVersion": "v1", "timeout": timeout_ms}
        if endpoint:
            http_options_kwargs["baseUrl"] = endpoint
        if not proxy:
            return types.HttpOptions(**http_options_kwargs)
        http_options_kwargs["clientArgs"] = {"proxy": proxy}
        http_options_kwargs["asyncClientArgs"] = {"proxy": proxy}
        return types.HttpOptions(**http_options_kwargs)

    @staticmethod
    def _close_genai_client(client) -> None:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _model_field(model, *names: str):
        for name in names:
            if isinstance(model, dict) and name in model:
                return model[name]
            value = getattr(model, name, None)
            if value is not None:
                return value
        return None

    @classmethod
    def _model_id_from_sdk_model(cls, model) -> str:
        raw_name = str(
            cls._model_field(model, "name", "model_id", "modelId", "publisher_model_id", "publisherModelId")
            or ""
        )
        return cls._normalize_sdk_model_name(raw_name)

    @classmethod
    def _sdk_model_supports_generate_content(cls, model) -> bool:
        methods = cls._model_field(model, "supported_generation_methods", "supportedGenerationMethods")
        if methods:
            return "generateContent" in methods or "generate_content" in methods

        supported_actions = cls._model_field(model, "supported_actions", "supportedActions") or {}
        if not supported_actions:
            return True

        open_api = cls._model_field(supported_actions, "open_api_spec", "openApiSpec") or {}
        endpoints = str(cls._model_field(open_api, "endpoints") or "")
        if "generateContent" in endpoints:
            return True

        method_names = str(cls._model_field(supported_actions, "method_names", "methodNames") or "")
        return "generateContent" in method_names

    async def fetch_models(self, credential: dict[str, Any]) -> list[str]:
        if self._is_vertexai(credential):
            return list(_VERTEXAI_MODELS)

        model_ids: list[str] = []
        client = self._build_genai_client(credential)
        aio_client = client.aio
        try:
            pager = await aio_client.models.list(config={"page_size": 100})
            async for item in pager:
                model_id = self._model_id_from_sdk_model(item)
                if model_id:
                    model_ids.append(model_id)
        finally:
            await aio_client.aclose()
            self._close_genai_client(client)
        return model_ids

    async def is_credential_available(self, credential: dict[str, Any]) -> bool:
        client = self._build_genai_client(credential)
        aio_client = client.aio
        try:
            probe_model = (
                _VERTEXAI_PROBE_MODEL
                if self._is_vertexai(credential)
                else await self._select_probe_model(aio_client)
            )
            if not probe_model:
                return False

            await aio_client.models.generate_content(
                model=probe_model,
                contents="ping",
                config=types.GenerateContentConfig(maxOutputTokens=1),
            )
            return True
        except Exception:
            return False
        finally:
            await aio_client.aclose()
            self._close_genai_client(client)

    async def _select_probe_model(self, aio_client) -> str | None:
        pager = await aio_client.models.list(config={"page_size": 100})
        async for item in pager:
            model_id = self._model_id_from_sdk_model(item)
            if model_id.startswith("gemini-") and self._sdk_model_supports_generate_content(item):
                return model_id
        return None

    async def explain_credential(self, credential: dict[str, Any]) -> dict:
        api_key = self._api_key(credential)
        info = {
            "provider": self.name,
            "status": "unknown",
            "model_source": self.model_source,
            "credential_hint": api_key[:8] + "***",
        }
        if self._is_vertexai(credential):
            info.update(
                {
                    "auth_type": "vertex_api_key",
                    "vertexai": True,
                    "base_url": self._base_url(credential),
                }
            )
            return info

        info["auth_type"] = "x-goog-api-key"
        info["base_url"] = self._base_url(credential)
        return info
