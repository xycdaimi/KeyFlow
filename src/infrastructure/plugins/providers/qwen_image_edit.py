"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-06-02
@Description: Qwen image edit account pool provider plugin using DashScope SDK
"""
from __future__ import annotations

from typing import Any

import httpx

from infrastructure.plugins.base import EgressMode, ProviderPlugin

try:
    import dashscope
    from dashscope import MultiModalConversation
except ImportError:  # pragma: no cover - exercised by runtime readiness checks
    dashscope = None
    MultiModalConversation = None

_BASE_URL = "https://dashscope.aliyuncs.com"
_KNOWN_MODELS: tuple[str, ...] = (
    "qwen-image-2.0-pro",
    "qwen-image-2.0-pro-2026-04-22",
    "qwen-image-2.0-pro-2026-03-03",
    "qwen-image-2.0",
    "qwen-image-2.0-2026-03-03",
    "qwen-image-edit-max",
    "qwen-image-edit-max-2026-01-16",
    "qwen-image-edit-plus",
    "qwen-image-edit-plus-2025-12-15",
    "qwen-image-edit-plus-2025-10-30",
    "qwen-image-edit",
)


class QwenImageEditPlugin(ProviderPlugin):
    PLUGIN_VERSION = "1.0.0"
    PLUGIN_INTERFACE_VERSION = "1.0.0"

    @property
    def name(self) -> str:
        return "qwen-image-edit"

    @property
    def description(self) -> str:
        return "Alibaba Cloud Model Studio Qwen image edit API via the official DashScope SDK."

    @property
    def auth_type(self) -> str:
        return "bearer_api_key"

    @property
    def credential_hint(self) -> str:
        return '{"api_key": "sk-..."} (DashScope API Key)'

    @property
    def egress_mode(self) -> EgressMode:
        return "direct"

    def is_plugin_ready(self) -> bool:
        return dashscope is not None and MultiModalConversation is not None

    @staticmethod
    def _api_key(credential: dict[str, Any]) -> str:
        return str(credential.get("api_key") or "").strip()

    async def verify_upstream_root_reachable(self) -> None:
        await self._ensure_upstream_root_http_reachable(_BASE_URL, httpx.AsyncClient)

    async def fetch_models(self, credential: dict[str, Any]) -> list[str]:
        return list(_KNOWN_MODELS)

    async def is_credential_available(self, credential: dict[str, Any]) -> bool:
        if not self.is_plugin_ready():
            return False
        return bool(self._api_key(credential))

    async def explain_credential(self, credential: dict[str, Any]) -> dict:
        api_key = self._api_key(credential)
        info = {
            "provider": self.name,
            "status": "unknown",
            "model_source": self.model_source,
            "auth_type": "bearer_api_key",
            "credential_hint": api_key[:8] + "***",
        }
        return info
