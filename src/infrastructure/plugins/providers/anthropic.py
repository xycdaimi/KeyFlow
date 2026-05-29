"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: Anthropic provider plugin
"""
from __future__ import annotations

from typing import Any

import httpx

from infrastructure.plugins.base import EgressMode, ProviderPlugin

_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicPlugin(ProviderPlugin):
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_INTERFACE_VERSION = "1.0.0"

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def description(self) -> str:
        return "Anthropic Claude API at api.anthropic.com."

    @property
    def auth_type(self) -> str:
        return "header_api_key"

    @property
    def credential_hint(self) -> str:
        return '{"api_key": "sk-ant-..."} (Anthropic API Key)'

    @property
    def egress_mode(self) -> EgressMode:
        return "proxy"

    @staticmethod
    def _api_key(credential: dict[str, Any]) -> str:
        return str(credential["api_key"])

    async def verify_upstream_root_reachable(self) -> None:
        await self._ensure_upstream_root_http_reachable(_BASE_URL, httpx.AsyncClient)

    async def fetch_models(self, credential: dict[str, Any]) -> list[str]:
        api_key = self._api_key(credential)
        model_ids: list[str] = []
        after_id: str | None = None

        async with self._build_http_client(httpx.AsyncClient) as client:
            while True:
                params: dict[str, str | int] = {"limit": 100}
                if after_id:
                    params["after_id"] = after_id

                response = await client.get(
                    f"{_BASE_URL}/v1/models",
                    headers={"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION},
                    params=params,
                )
                response.raise_for_status()
                body = response.json()
                for item in body.get("data", []):
                    model_ids.append(item["id"])
                if not body.get("has_more"):
                    break
                after_id = body.get("last_id")
                if not after_id:
                    break

        return model_ids

    async def is_credential_available(self, credential: dict[str, Any]) -> bool:
        api_key = self._api_key(credential)
        async with self._build_http_client(httpx.AsyncClient) as client:
            response = await client.get(
                f"{_BASE_URL}/v1/models",
                headers={"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION},
                params={"limit": 1},
            )
            if response.status_code in (401, 403):
                return False
            return True

    async def explain_credential(self, credential: dict[str, Any]) -> dict:
        api_key = self._api_key(credential)
        return {
            "provider": self.name,
            "status": "unknown",
            "model_source": self.model_source,
            "auth_type": "x-api-key",
            "credential_hint": api_key[:8] + "***",
        }
