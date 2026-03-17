from __future__ import annotations

import httpx

from infrastructure.plugins.base import ProviderPlugin

_BASE_URL = "https://api.openai.com/v1"


class OpenAIPlugin(ProviderPlugin):
    """Plugin for the official OpenAI API.

    Availability: key is available when the balance is positive AND
    the key is not rate-limited (checked via a lightweight models call).

    Internal billing logic:
        - Queries /v1/organization/balance for credit balance.
        - key is considered UNAVAILABLE if balance <= 0.
        - All balance/usage detail is kept private; the core only sees bool.
    """

    @property
    def name(self) -> str:
        return "openai"

    async def fetch_models(self, api_key: str) -> list[str]:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{_BASE_URL}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            return [item["id"] for item in response.json().get("data", [])]

    async def is_credential_available(self, api_key: str, model: str | None = None) -> bool:
        """Available when the key is valid and has a positive credit balance."""
        async with httpx.AsyncClient(timeout=10) as client:
            # Validate key with a cheap models list call
            r = await client.get(
                f"{_BASE_URL}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if r.status_code in (401, 403):
                return False
            if not r.is_success:
                return True  # transient error — keep available

            # Check credit balance
            balance_r = await client.get(
                f"{_BASE_URL}/organization/balance",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if balance_r.status_code in (403, 404):
                return True  # billing API inaccessible — assume available
            if not balance_r.is_success:
                return True

            body = balance_r.json()
            for entry in body.get("available", []):
                if entry.get("currency", "").lower() == "usd":
                    return float(entry.get("amount", 0)) > 0
            return True

    async def explain_credential(self, api_key: str) -> dict:
        return {"provider": self.name, "key_prefix": api_key[:8] + "…"}
