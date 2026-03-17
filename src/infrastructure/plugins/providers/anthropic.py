from __future__ import annotations

import httpx

from infrastructure.plugins.base import ProviderPlugin

_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicPlugin(ProviderPlugin):
    """Plugin for Anthropic Claude.

    Availability: key is valid when the models endpoint returns 200.
    Anthropic does not expose a public balance API; availability is based
    solely on authentication success.
    """

    @property
    def name(self) -> str:
        return "anthropic"

    async def fetch_models(self, api_key: str) -> list[str]:
        model_ids: list[str] = []
        after_id: str | None = None

        async with httpx.AsyncClient(timeout=15) as client:
            while True:
                params: dict[str, str | int] = {"limit": 100}
                if after_id:
                    params["after_id"] = after_id

                r = await client.get(
                    f"{_BASE_URL}/v1/models",
                    headers={"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION},
                    params=params,
                )
                r.raise_for_status()
                body = r.json()
                for item in body.get("data", []):
                    model_ids.append(item["id"])
                if not body.get("has_more"):
                    break
                after_id = body.get("last_id")
                if not after_id:
                    break

        return model_ids

    async def is_credential_available(self, api_key: str, model: str | None = None) -> bool:
        """Available when the API key authenticates successfully."""
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_BASE_URL}/v1/models",
                headers={"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION},
                params={"limit": 1},
            )
            if r.status_code in (401, 403):
                return False
            return True  # 200 or transient error → keep available

    async def explain_credential(self, api_key: str) -> dict:
        return {"provider": self.name, "key_prefix": api_key[:8] + "…"}
