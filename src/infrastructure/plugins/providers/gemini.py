"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-03
@Description: Google Gemini 官方 API 供应商插件
"""
from __future__ import annotations

import httpx

from infrastructure.plugins.base import ProviderPlugin, ensure_upstream_root_http_reachable

_BASE_URL = "https://generativelanguage.googleapis.com"


class GeminiPlugin(ProviderPlugin):
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_INTERFACE_VERSION = "1.0.0"

    """Plugin for Google Gemini (generativelanguage API / AI Studio).

    Availability: key is valid when the models endpoint returns 200.
    Google does not expose a per-key credit balance API; availability
    is based solely on authentication success.
    """

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def description(self) -> str:
        return (
            "Google Gemini 官方 API（generativelanguage.googleapis.com / AI Studio）。"
            "可用性取决于 API Key 是否通过身份验证（Google 无公开的 per-key 余额接口）。"
        )

    @property
    def auth_type(self) -> str:
        return "header_api_key"

    @property
    def credential_hint(self) -> str:
        return '{"api_key": "AIza..."}（Google AI Studio API Key）'

    @staticmethod
    def _api_key(credential: dict[str, str]) -> str:
        return credential["api_key"]

    async def verify_upstream_root_reachable(self) -> None:
        await ensure_upstream_root_http_reachable(_BASE_URL)

    @staticmethod
    def _availability_status(response: httpx.Response) -> str:
        if response.is_success:
            return "AVAILABLE"

        code = response.status_code
        if code == 403:
            return "UNAVAILABLE_PERMISSION"
        if code == 400:
            return "UNAVAILABLE_CONFIG"
        if code == 429:
            return "TEMP_UNAVAILABLE"
        if code == 500:
            return "TEMP_UNAVAILABLE"
        return "UNKNOWN"

    @staticmethod
    def _normalize_model_name(model: str) -> str:
        return model if model.startswith("models/") else f"models/{model}"

    async def _select_probe_model(self, client: httpx.AsyncClient, api_key: str) -> str | None:
        page_token: str | None = None

        while True:
            params: dict[str, str | int] = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token

            r = await client.get(
                f"{_BASE_URL}/v1beta/models",
                headers={"x-goog-api-key": api_key},
                params=params,
            )
            if not r.is_success:
                return None

            body = r.json()
            for item in body.get("models", []):
                raw_name = str(item.get("name") or "")
                model_id = raw_name.removeprefix("models/")
                if not model_id:
                    continue
                methods = item.get("supportedGenerationMethods") or []
                if "generateContent" in methods:
                    return model_id

            page_token = body.get("nextPageToken")
            if not page_token:
                return None

    async def fetch_models(self, credential: dict[str, str]) -> list[str]:
        api_key = self._api_key(credential)
        model_ids: list[str] = []
        page_token: str | None = None

        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                params: dict[str, str | int] = {"pageSize": 100}
                if page_token:
                    params["pageToken"] = page_token

                r = await client.get(
                    f"{_BASE_URL}/v1beta/models",
                    headers={"x-goog-api-key": api_key},
                    params=params,
                )
                r.raise_for_status()
                body = r.json()

                for item in body.get("models", []):
                    raw_name: str = item.get("name", "")
                    model_id = raw_name.removeprefix("models/")
                    if model_id:
                        model_ids.append(model_id)

                page_token = body.get("nextPageToken")
                if not page_token:
                    break

        return model_ids

    async def is_credential_available(self, credential: dict[str, str], model: str | None = None) -> bool:
        """Gemini availability is based on whether a minimal inference request works."""
        api_key = self._api_key(credential)
        async with httpx.AsyncClient(timeout=10) as client:
            probe_model = model or await self._select_probe_model(client, api_key)
            if not probe_model:
                return False

            r = await client.post(
                f"{_BASE_URL}/v1beta/{self._normalize_model_name(probe_model)}:generateContent",
                headers={"x-goog-api-key": api_key},
                json={
                    "contents": [{"parts": [{"text": "ping"}]}],
                    "generationConfig": {"maxOutputTokens": 1},
                },
            )
            return self._availability_status(r) == "AVAILABLE"

    async def explain_credential(self, credential: dict[str, str]) -> dict:
        api_key = self._api_key(credential)
        return {
            "provider": self.name,
            "status": "unknown",
            "model_source": self.model_source,
            "auth_type": "x-goog-api-key",
            "credential_hint": api_key[:8] + "***",
        }
