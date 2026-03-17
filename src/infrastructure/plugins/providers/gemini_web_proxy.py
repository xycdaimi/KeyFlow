from __future__ import annotations

import logging

from infrastructure.plugins.base import ProviderPlugin

logger = logging.getLogger(__name__)

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

    Credential format: the ``api_key`` stored in KeyFlow is the
    ``__Secure-1PSID`` cookie value. This cookie rotates; callers should
    refresh it via the update-key endpoint when it expires.

    Availability check: attempts a minimal library init to verify the cookie
    is still valid. Returns False on auth errors; True on success.

    Balance / quota: cookies have no billing concept. Availability is
    determined solely by cookie validity.
    """

    @property
    def name(self) -> str:
        return "gemini-web-proxy"

    async def fetch_models(self, api_key: str) -> list[str]:
        """Return the static list of known Gemini Web models.

        The gemini_webapi library does not provide a model-list endpoint.
        """
        return list(_KNOWN_MODELS)

    async def is_credential_available(self, api_key: str, model: str | None = None) -> bool:
        """Verify the cookie is still valid using the gemini_webapi library."""
        try:
            from gemini_webapi import GeminiClient  # type: ignore[import]
        except ImportError:
            logger.warning(
                "gemini-webapi not installed. "
                "Run 'pip install gemini-webapi' to enable availability checks."
            )
            return False

        try:
            client = GeminiClient(secure_1psid=api_key)
            await client.init(timeout=10, auto_close=True, close_delay=0)
            return True
        except Exception as exc:
            logger.debug("GeminiWebProxyPlugin cookie check failed: %s", exc)
            return False

    async def explain_credential(self, api_key: str) -> dict:
        return {
            "provider": self.name,
            "auth_type": "cookie (__Secure-1PSID)",
            "cookie_prefix": api_key[:12] + "…",
        }
