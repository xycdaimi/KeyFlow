from __future__ import annotations

import httpx

from infrastructure.plugins.base import ProviderPlugin

_BASE_URL = "https://openrouter.ai"


class OpenRouterPlugin(ProviderPlugin):
    """Plugin for OpenRouter.

    Availability: key is valid AND has remaining credit balance.

    Internal billing logic:
        - Queries /api/v1/auth/key for (limit, usage).
        - Available only when remaining = limit - usage > 0 (or free tier).
        - All billing detail is private; the core only sees bool.
    """

    @property
    def name(self) -> str:
        return "openrouter"

    async def fetch_models(self, api_key: str) -> list[str]:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{_BASE_URL}/api/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            r.raise_for_status()
            return [item["id"] for item in r.json().get("data", [])]

    async def is_credential_available(self, api_key: str, model: str | None = None) -> bool:
        """Available when the key is valid and has positive credit balance."""
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_BASE_URL}/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if r.status_code in (401, 403):
                return False
            if not r.is_success:
                return True  # transient — keep available

            data = r.json().get("data", {})
            if data.get("is_free_tier"):
                return True
            limit = float(data.get("limit") or 0)
            usage = float(data.get("usage") or 0)
            return (limit - usage) > 0

    async def explain_credential(self, api_key: str) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_BASE_URL}/api/v1/auth/key",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if not r.is_success:
                return {"provider": self.name, "status": "unknown"}
            data = r.json().get("data", {})
            limit = float(data.get("limit") or 0)
            usage = float(data.get("usage") or 0)
            return {
                "provider": self.name,
                "is_free_tier": data.get("is_free_tier", False),
                "remaining_usd": round(limit - usage, 4),
                "label": data.get("label", ""),
            }
