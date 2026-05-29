"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: OpenRouter provider plugin
"""
from __future__ import annotations

from typing import Any

import httpx

from infrastructure.plugins.base import CapacitySignal, EgressMode, ProviderPlugin

_BASE_URL = "https://openrouter.ai"


class OpenRouterPlugin(ProviderPlugin):
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_INTERFACE_VERSION = "1.0.0"

    @property
    def name(self) -> str:
        return "openrouter"

    @property
    def description(self) -> str:
        return "OpenRouter API at openrouter.ai."

    @property
    def auth_type(self) -> str:
        return "bearer_api_key"

    @property
    def credential_hint(self) -> str:
        return '{"api_key": "sk-or-..."} (OpenRouter API Key, Bearer token)'

    @property
    def egress_mode(self) -> EgressMode:
        return "proxy"

    @staticmethod
    def _api_key(credential: dict[str, Any]) -> str:
        return str(credential["api_key"])

    async def verify_upstream_root_reachable(self) -> None:
        await self._ensure_upstream_root_http_reachable(_BASE_URL, httpx.AsyncClient)

    @staticmethod
    def _remaining_budget(data: dict) -> float:
        limit = float(data.get("limit") or 0)
        limit_reset = str(data.get("limit_reset") or "").lower()
        usage_field_by_reset = {
            "daily": "usage_daily",
            "weekly": "usage_weekly",
            "monthly": "usage_monthly",
        }
        usage_field = usage_field_by_reset.get(limit_reset, "usage")
        usage = float(data.get(usage_field) or 0)
        return limit - usage

    async def get_capacity_signal(self, credential: dict[str, Any]) -> CapacitySignal | None:
        api_key = self._api_key(credential)
        async with self._build_http_client(httpx.AsyncClient) as client:
            response = await client.get(
                f"{_BASE_URL}/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if not response.is_success:
                return None

            data = response.json().get("data", {})
            limit = float(data.get("limit") or 0)
            remaining = self._remaining_budget(data)
            score = 1.0 if limit <= 0 else min(max(remaining / limit, 0.0), 1.0)
            return CapacitySignal(
                has_capacity_signal=True,
                capacity_score=score,
                quota_available=(True if data.get("is_free_tier") else (remaining > 0)),
                capacity_kind="remaining_budget_ratio",
                reason=f"limit_reset={data.get('limit_reset', 'unknown')}",
            )

    async def fetch_models(self, credential: dict[str, Any]) -> list[str]:
        api_key = self._api_key(credential)
        async with self._build_http_client(httpx.AsyncClient) as client:
            response = await client.get(
                f"{_BASE_URL}/api/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            return [item["id"] for item in response.json().get("data", [])]

    async def is_credential_available(self, credential: dict[str, Any]) -> bool:
        api_key = self._api_key(credential)
        async with self._build_http_client(httpx.AsyncClient) as client:
            response = await client.get(
                f"{_BASE_URL}/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if response.status_code in (401, 403):
                return False
            return True

    async def explain_credential(self, credential: dict[str, Any]) -> dict:
        api_key = self._api_key(credential)
        masked_credential = api_key[:8] + "***"
        async with self._build_http_client(httpx.AsyncClient) as client:
            response = await client.get(
                f"{_BASE_URL}/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if not response.is_success:
                return {
                    "provider": self.name,
                    "status": "unknown",
                    "model_source": self.model_source,
                    "auth_type": "bearer_api_key",
                    "credential_hint": masked_credential,
                }
            data = response.json().get("data", {})
            return {
                "provider": self.name,
                "status": "ok",
                "model_source": self.model_source,
                "auth_type": "bearer_api_key",
                "credential_hint": masked_credential,
                "is_free_tier": data.get("is_free_tier", False),
                "remaining_usd": round(self._remaining_budget(data), 4),
                "label": data.get("label", ""),
            }
