"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-22
@Description: Gemini Web（Cookie / gemini-webapi）供应商插件
"""
from __future__ import annotations

import logging

import httpx

from infrastructure.plugins.base import EgressMode, ProviderPlugin

logger = logging.getLogger(__name__)

_GEMINI_WEB_ORIGIN = "https://gemini.google.com"

_KNOWN_MODELS: list[str] = [
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-2.0-flash-thinking-exp",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
]


class GeminiWebProxyPlugin(ProviderPlugin):
    """Plugin for Gemini Web API access via the ``gemini_webapi`` library.

    ``HanaokaYuzu/Gemini-API`` (pip: ``gemini-webapi``) is a Python **library**
    that drives the Gemini web interface using browser cookies.

    Credential format: KeyFlow stores a structured credential dict containing
    both ``secure_1psid`` and ``secure_1psidts`` cookie values.

    Availability check: attempts a minimal library init to verify the cookie
    is still valid. Returns False on auth errors; True on success.

    Balance / quota: cookies have no billing concept. Availability is
    determined solely by cookie validity.
    """

    PLUGIN_VERSION = "1.0.0"
    PLUGIN_INTERFACE_VERSION = "1.0.0"

    @property
    def name(self) -> str:
        return "gemini-web-proxy"

    @property
    def description(self) -> str:
        return (
            "通过 gemini-webapi 库以浏览器 Cookie 方式访问 Gemini Web 界面（非官方）。"
            "需要额外安装 gemini-webapi 依赖（pip install gemini-webapi）。"
            "可用性取决于 Cookie 是否仍有效，Cookie 会定期轮换，失效后需手动更新。"
        )

    @property
    def auth_type(self) -> str:
        return "cookie"

    @property
    def credential_hint(self) -> str:
        return '{"secure_1psid": "...", "secure_1psidts": "..."}'

    @property
    def model_source(self) -> str:
        return "static"

    @property
    def egress_mode(self) -> EgressMode:
        return "proxy"

    def _is_dependency_available(self) -> bool:
        try:
            from gemini_webapi import GeminiClient  # type: ignore[import]
        except ImportError:
            return False
        return GeminiClient is not None

    def is_plugin_ready(self) -> bool:
        return self._is_dependency_available()

    async def verify_upstream_root_reachable(self) -> None:
        await self._ensure_upstream_root_http_reachable(_GEMINI_WEB_ORIGIN, httpx.AsyncClient)

    async def fetch_models(self, credential: dict[str, str]) -> list[str]:
        """Return the static list of known Gemini Web models.

        The gemini_webapi library does not provide a model-list endpoint.
        """
        return list(_KNOWN_MODELS)

    async def is_credential_available(self, credential: dict[str, str]) -> bool:
        """Verify the cookie is still valid using the gemini_webapi library."""
        if not self._is_dependency_available():
            logger.warning(
                "gemini-webapi not installed. "
                "Run 'pip install gemini-webapi' to enable availability checks."
            )
            return False

        try:
            from gemini_webapi import GeminiClient  # type: ignore[import]
            secure_1psid = credential["secure_1psid"]
            secure_1psidts = credential["secure_1psidts"]
            client = GeminiClient(
                secure_1psid=secure_1psid,
                secure_1psidts=secure_1psidts,
                proxy=self._proxy_url(),
            )
            await client.init(timeout=10, auto_close=True, close_delay=0)
            return True
        except Exception as exc:
            logger.debug("GeminiWebProxyPlugin cookie check failed: %s", exc)
            return False

    async def explain_credential(self, credential: dict[str, str]) -> dict:
        dependency_available = self._is_dependency_available()
        secure_1psid = credential.get("secure_1psid", "")
        secure_1psidts = credential.get("secure_1psidts", "")
        return {
            "provider": self.name,
            "status": "unknown" if dependency_available else "dependency_missing",
            "model_source": self.model_source,
            "auth_type": "cookie",
            "credential_hint": {
                "secure_1psid": secure_1psid[:12] + "***" if secure_1psid else "missing",
                "secure_1psidts": secure_1psidts[:12] + "***" if secure_1psidts else "missing",
            },
            "dependency": {
                "name": "gemini-webapi",
                "installed": dependency_available,
                "degrade_strategy": "not_allocatable_when_dependency_missing",
            },
        }
