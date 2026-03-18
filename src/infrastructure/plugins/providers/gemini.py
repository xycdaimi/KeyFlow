from __future__ import annotations

import httpx

from infrastructure.plugins.base import ProviderPlugin

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

    async def fetch_models(self, api_key: str) -> list[str]:
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

    async def is_credential_available(self, api_key: str, model: str | None = None) -> bool:
        """Available when the API key authenticates successfully."""
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_BASE_URL}/v1beta/models",
                headers={"x-goog-api-key": api_key},
                params={"pageSize": 1},
            )
            if r.status_code in (400, 401, 403):
                return False
            return True

    async def explain_credential(self, api_key: str) -> dict:
        return {
            "provider": self.name,
            "status": "unknown",
            "model_source": self.model_source,
            "auth_type": "x-goog-api-key",
            "credential_hint": api_key[:8] + "***",
        }
