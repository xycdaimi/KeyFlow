"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: OpenAI provider plugin
"""
from __future__ import annotations

from typing import Any

import httpx

from infrastructure.plugins.base import CapacitySignal, EgressMode, ProviderPlugin

_ROOT_ORIGIN = "https://api.openai.com"
_BASE_URL = f"{_ROOT_ORIGIN}/v1"


class OpenAIPlugin(ProviderPlugin):
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_INTERFACE_VERSION = "1.0.0"

    @property
    def name(self) -> str:
        return "openai"

    @property
    def description(self) -> str:
        return "OpenAI official API at api.openai.com."

    @property
    def auth_type(self) -> str:
        return "bearer_api_key"

    @property
    def credential_hint(self) -> str:
        return '{"api_key": "sk-..."} (OpenAI API Key, Bearer token)'

    @property
    def egress_mode(self) -> EgressMode:
        return "proxy"

    @staticmethod
    def _api_key(credential: dict[str, Any]) -> str:
        return str(credential["api_key"])

    async def verify_upstream_root_reachable(self) -> None:
        await self._ensure_upstream_root_http_reachable(_ROOT_ORIGIN, httpx.AsyncClient)

    @staticmethod
    def _error_payload_text(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except Exception:
            return ""
        error = payload.get("error")
        if isinstance(error, dict):
            parts = [
                str(error.get("message") or ""),
                str(error.get("type") or ""),
                str(error.get("code") or ""),
            ]
            return " ".join(parts).lower()
        return str(payload).lower()

    @classmethod
    def _is_quota_exhausted_error(cls, response: httpx.Response) -> bool:
        if response.status_code != 429:
            return False
        error_text = cls._error_payload_text(response)
        quota_markers = (
            "quota",
            "insufficient_quota",
            "exceeded your current quota",
            "billing",
            "credit balance",
        )
        return any(marker in error_text for marker in quota_markers)

    async def fetch_models(self, credential: dict[str, Any]) -> list[str]:
        api_key = self._api_key(credential)
        async with self._build_http_client(httpx.AsyncClient) as client:
            response = await client.get(
                f"{_BASE_URL}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            return [item["id"] for item in response.json().get("data", [])]

    async def is_credential_available(self, credential: dict[str, Any]) -> bool:
        api_key = self._api_key(credential)
        async with self._build_http_client(httpx.AsyncClient) as client:
            response = await client.get(
                f"{_BASE_URL}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if response.status_code in (401, 403):
                return False
            return True

    async def get_capacity_signal(self, credential: dict[str, Any]) -> CapacitySignal | None:
        api_key = self._api_key(credential)
        try:
            async with self._build_http_client(httpx.AsyncClient) as client:
                response = await client.get(
                    f"{_BASE_URL}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
        except Exception:
            return None
        if response.status_code in (401, 403):
            return None
        if self._is_quota_exhausted_error(response):
            return CapacitySignal(
                has_capacity_signal=True,
                capacity_score=0.0,
                quota_available=False,
                capacity_kind="quota_error",
                reason="insufficient_quota",
            )
        if response.is_success:
            return CapacitySignal(
                has_capacity_signal=False,
                capacity_score=None,
                quota_available=True,
                capacity_kind="unknown",
                reason="models_endpoint_success",
            )
        return None

    async def explain_credential(self, credential: dict[str, Any]) -> dict:
        api_key = self._api_key(credential)
        return {
            "provider": self.name,
            "status": "unknown",
            "model_source": self.model_source,
            "auth_type": "bearer_api_key",
            "credential_hint": api_key[:8] + "***",
        }
