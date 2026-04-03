"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-03
@Description: Anthropic Claude API 供应商插件
"""
from __future__ import annotations

import httpx

from infrastructure.plugins.base import ProviderPlugin, ensure_upstream_root_http_reachable

_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicPlugin(ProviderPlugin):
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_INTERFACE_VERSION = "1.0.0"

    """Plugin for Anthropic Claude.

    Availability: key is valid when the models endpoint returns 200.
    Anthropic does not expose a public balance API; availability is based
    solely on authentication success.
    """

    @property
    def name(self) -> str:
        return "anthropic"

    @property
    def description(self) -> str:
        return (
            "Anthropic Claude API（api.anthropic.com）。"
            "可用性取决于 API Key 是否通过身份验证（Anthropic 无公开余额查询接口）。"
        )

    @property
    def auth_type(self) -> str:
        return "header_api_key"

    @property
    def credential_hint(self) -> str:
        return '{"api_key": "sk-ant-..."}（Anthropic API Key）'

    @staticmethod
    def _api_key(credential: dict[str, str]) -> str:
        return credential["api_key"]

    async def verify_upstream_root_reachable(self) -> None:
        await ensure_upstream_root_http_reachable(_BASE_URL)

    async def fetch_models(self, credential: dict[str, str]) -> list[str]:
        api_key = self._api_key(credential)
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

    async def is_credential_available(self, credential: dict[str, str], model: str | None = None) -> bool:
        """Available when the API key authenticates successfully."""
        api_key = self._api_key(credential)
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{_BASE_URL}/v1/models",
                headers={"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION},
                params={"limit": 1},
            )
            if r.status_code in (401, 403):
                return False
            return True  # 200 or transient error → keep available

    async def explain_credential(self, credential: dict[str, str]) -> dict:
        api_key = self._api_key(credential)
        return {
            "provider": self.name,
            "status": "unknown",
            "model_source": self.model_source,
            "auth_type": "x-api-key",
            "credential_hint": api_key[:8] + "***",
        }
