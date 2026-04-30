"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-16
@Description: Google Gemini official API provider plugin
"""
from __future__ import annotations

import httpx

from infrastructure.plugins.base import EgressMode, ProviderPlugin

_BASE_URL = "https://generativelanguage.googleapis.com"


class GeminiPlugin(ProviderPlugin):
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_INTERFACE_VERSION = "1.0.0"

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def description(self) -> str:
        return "Google Gemini official API at generativelanguage.googleapis.com."

    @property
    def auth_type(self) -> str:
        return "header_api_key"

    @property
    def credential_hint(self) -> str:
        return '{"api_key": "AIza..."} (Google AI Studio API Key)'

    @property
    def egress_mode(self) -> EgressMode:
        return "proxy"

    @staticmethod
    def _api_key(credential: dict[str, str]) -> str:
        return credential["api_key"]

    async def verify_upstream_root_reachable(self) -> None:
        await self._ensure_upstream_root_http_reachable(_BASE_URL, httpx.AsyncClient)

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

            response = await client.get(
                f"{_BASE_URL}/v1beta/models",
                headers={"x-goog-api-key": api_key},
                params=params,
            )
            if not response.is_success:
                return None

            body = response.json()
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

        async with self._build_http_client(httpx.AsyncClient) as client:
            while True:
                params: dict[str, str | int] = {"pageSize": 100}
                if page_token:
                    params["pageToken"] = page_token

                response = await client.get(
                    f"{_BASE_URL}/v1beta/models",
                    headers={"x-goog-api-key": api_key},
                    params=params,
                )
                response.raise_for_status()
                body = response.json()

                for item in body.get("models", []):
                    raw_name: str = item.get("name", "")
                    model_id = raw_name.removeprefix("models/")
                    if model_id:
                        model_ids.append(model_id)

                page_token = body.get("nextPageToken")
                if not page_token:
                    break

        return model_ids

    async def is_credential_available(self, credential: dict[str, str]) -> bool:
        api_key = self._api_key(credential)
        async with self._build_http_client(httpx.AsyncClient) as client:
            probe_model = await self._select_probe_model(client, api_key)
            if not probe_model:
                return False

            response = await client.post(
                f"{_BASE_URL}/v1beta/{self._normalize_model_name(probe_model)}:generateContent",
                headers={"x-goog-api-key": api_key},
                json={
                    "contents": [{"parts": [{"text": "ping"}]}],
                    "generationConfig": {"maxOutputTokens": 1},
                },
            )
            return self._availability_status(response) == "AVAILABLE"

    async def explain_credential(self, credential: dict[str, str]) -> dict:
        api_key = self._api_key(credential)
        return {
            "provider": self.name,
            "status": "unknown",
            "model_source": self.model_source,
            "auth_type": "x-goog-api-key",
            "credential_hint": api_key[:8] + "***",
        }
