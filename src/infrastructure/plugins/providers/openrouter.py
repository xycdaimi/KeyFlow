from __future__ import annotations

import httpx

from infrastructure.plugins.base import CapacitySignal, ProviderPlugin

_BASE_URL = "https://openrouter.ai"


class OpenRouterPlugin(ProviderPlugin):
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_INTERFACE_VERSION = "1.0.0"

    """Plugin for OpenRouter.

    Credential availability: key is valid/reachable.
    Quota availability: exposed via get_capacity_signal.

    Internal billing logic:
        - Queries /api/v1/auth/key for (limit, usage).
        - Available only when remaining = limit - usage > 0 (or free tier).
        - All billing detail is private; the core only sees bool.
    """

    @property
    def name(self) -> str:
        return "openrouter"

    @property
    def description(self) -> str:
        return (
            "OpenRouter 聚合 API（openrouter.ai），支持多家模型厂商。"
            "可用性取决于 API Key 有效且账户剩余额度大于 0（免费套餐除外）。"
        )

    @property
    def auth_type(self) -> str:
        return "bearer_api_key"

    @property
    def credential_hint(self) -> str:
        return '{"api_key": "sk-or-..."}（OpenRouter API Key，Bearer 令牌）'

    @staticmethod
    def _api_key(credential: dict[str, str]) -> str:
        return credential["api_key"]

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

    async def get_capacity_signal(self, credential: dict[str, str]) -> CapacitySignal | None:
        api_key = self._api_key(credential)
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_BASE_URL}/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if not r.is_success:
                return None

            data = r.json().get("data", {})
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

    async def fetch_models(self, credential: dict[str, str]) -> list[str]:
        api_key = self._api_key(credential)
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{_BASE_URL}/api/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            r.raise_for_status()
            return [item["id"] for item in r.json().get("data", [])]

    async def is_credential_available(self, credential: dict[str, str], model: str | None = None) -> bool:
        """Available when the credential itself is valid/reachable."""
        api_key = self._api_key(credential)
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_BASE_URL}/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if r.status_code in (401, 403):
                return False
            return True

    async def explain_credential(self, credential: dict[str, str]) -> dict:
        api_key = self._api_key(credential)
        masked_credential = api_key[:8] + "***"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_BASE_URL}/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if not r.is_success:
                return {
                    "provider": self.name,
                    "status": "unknown",
                    "model_source": self.model_source,
                    "auth_type": "bearer_api_key",
                    "credential_hint": masked_credential,
                }
            data = r.json().get("data", {})
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
