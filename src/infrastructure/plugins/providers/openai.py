from __future__ import annotations

import httpx

from infrastructure.plugins.base import ProviderPlugin

_BASE_URL = "https://api.openai.com/v1"


class OpenAIPlugin(ProviderPlugin):
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_INTERFACE_VERSION = "1.0.0"

    """Plugin for the official OpenAI API.

    Availability is inferred from a lightweight API request.

    The current implementation validates the credential through ``/v1/models``:
        - 200 -> VALID
        - 401 -> INVALID_KEY
        - 429 + quota semantics -> NO_BALANCE
        - 429 + other semantics -> RATE_LIMIT
        - others -> UNKNOWN
    """

    @property
    def name(self) -> str:
        return "openai"

    @property
    def description(self) -> str:
        return (
            "OpenAI 官方 API（api.openai.com）。"
            "可用性取决于轻量 API 请求是否成功，401/403/429 视为当前不可用。"
        )

    @property
    def auth_type(self) -> str:
        return "bearer_api_key"

    @property
    def credential_hint(self) -> str:
        return '{"api_key": "sk-..."}（OpenAI API Key，Bearer 令牌）'

    @staticmethod
    def _api_key(credential: dict[str, str]) -> str:
        return credential["api_key"]

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
    def _availability_status(cls, response: httpx.Response) -> str:
        if response.is_success:
            return "VALID"

        if response.status_code in (401, 403):
            return "INVALID_KEY"

        if response.status_code == 429:
            error_text = cls._error_payload_text(response)
            quota_markers = (
                "quota",
                "insufficient_quota",
                "exceeded your current quota",
                "billing",
                "credit balance",
            )
            if any(marker in error_text for marker in quota_markers):
                return "NO_BALANCE"
            return "RATE_LIMIT"

        return "UNKNOWN"

    async def fetch_models(self, credential: dict[str, str]) -> list[str]:
        api_key = self._api_key(credential)
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"{_BASE_URL}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            return [item["id"] for item in response.json().get("data", [])]

    async def is_credential_available(self, credential: dict[str, str], model: str | None = None) -> bool:
        """OpenAI key availability is inferred from one lightweight API request."""
        api_key = self._api_key(credential)
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_BASE_URL}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            return self._availability_status(r) in {"VALID", "UNKNOWN"}

    async def explain_credential(self, credential: dict[str, str]) -> dict:
        api_key = self._api_key(credential)
        return {
            "provider": self.name,
            "status": "unknown",
            "model_source": self.model_source,
            "auth_type": "bearer_api_key",
            "credential_hint": api_key[:8] + "***",
        }
