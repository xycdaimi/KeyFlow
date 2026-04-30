"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-23
@Description: 供应商插件抽象基类与注册表
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

import httpx

from domain.exceptions.domain_exceptions import UpstreamUnreachableError
from infrastructure.config.settings import get_settings

PLUGIN_INTERFACE_VERSION = "1.0.0"
ModelSource = Literal["remote", "static"]
CredentialDict = dict[str, str]
EgressMode = Literal["direct", "proxy"]

_UPSTREAM_ROOT_TIMEOUT_SECONDS = 5.0
_UPSTREAM_ROOT_HTTP_TIMEOUT = httpx.Timeout(_UPSTREAM_ROOT_TIMEOUT_SECONDS)


def build_provider_http_timeout(total_timeout: float | None = None) -> httpx.Timeout:
    settings = get_settings()
    total = settings.http_total_timeout if total_timeout is None else max(total_timeout, 0.1)
    connect = max(settings.http_connect_timeout, 0.1)
    read = max(settings.http_read_timeout, 0.1)
    return httpx.Timeout(
        total,
        connect=connect,
        read=read,
        write=read,
        pool=connect,
    )


@dataclass(slots=True)
class CapacitySignal:
    has_capacity_signal: bool
    capacity_score: float | None
    quota_available: bool | None = None
    capacity_kind: str = "unknown"
    reason: str = ""


@dataclass(slots=True)
class CredentialPreparationResult:
    credential: CredentialDict
    changed: bool = False


class ProviderPlugin(ABC):
    """Abstract base for all provider plugins.

    One plugin == one provider.

    The contract the core sees is minimal and strict:
        - fetch_models: called once when a credential is registered
        - is_credential_available: credential-level availability only
        - mark_success / mark_error: outcome callbacks; plugin updates its own internal state
        - explain_credential: optional admin display without sensitive fields

    Everything else (balance, pricing, quota, usage) is private to the plugin.
    The core must never call or depend on billing logic directly.
    """

    PLUGIN_VERSION = "1.0.0"
    PLUGIN_INTERFACE_VERSION = PLUGIN_INTERFACE_VERSION

    @property
    @abstractmethod
    def name(self) -> str:
        """Canonical provider identifier, for example 'openai'."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of this provider and its credential."""

    @property
    @abstractmethod
    def auth_type(self) -> str:
        """Authentication mechanism, for example 'bearer_api_key'."""

    @property
    @abstractmethod
    def credential_hint(self) -> str:
        """Example or pattern of the credential payload."""

    @property
    def model_source(self) -> ModelSource:
        """Where provider execution comes from.

        - remote: external API / service
        - static: local SDK / local code implementation
        """
        return "remote"

    @property
    def egress_mode(self) -> EgressMode:
        """Outbound network policy used by this provider plugin."""
        return "direct"

    def is_plugin_ready(self) -> bool:
        """Return True if plugin runtime dependencies are satisfied."""
        return True

    def _proxy_url(self) -> str | None:
        settings = get_settings()
        if self.egress_mode != "proxy":
            return None
        proxy = (settings.global_http_proxy or "").strip()
        if not proxy:
            return None
        return proxy

    def _build_http_client(
        self,
        client_factory,
        *,
        total_timeout: float | None = None,
        follow_redirects: bool = False,
        **kwargs,
    ):
        proxy = self._proxy_url()
        client_kwargs = {
            "timeout": build_provider_http_timeout(total_timeout),
            **kwargs,
        }
        if follow_redirects:
            client_kwargs["follow_redirects"] = True
        if proxy:
            client_kwargs["proxy"] = proxy
        return client_factory(**client_kwargs)

    async def _ensure_upstream_root_http_reachable(self, origin: str, client_factory) -> None:
        """GET ``{origin}/`` with no auth. Any HTTP response counts as reachable."""
        root = origin.rstrip("/") + "/"
        try:
            async with self._build_http_client(
                client_factory,
                total_timeout=_UPSTREAM_ROOT_HTTP_TIMEOUT.connect,
                follow_redirects=True,
            ) as client:
                await client.get(root)
        except httpx.RequestError as exc:
            raise UpstreamUnreachableError(root) from exc

    @abstractmethod
    async def fetch_models(self, credential: CredentialDict) -> list[str]:
        """Return model IDs available for this credential."""

    @abstractmethod
    async def is_credential_available(self, credential: CredentialDict) -> bool:
        """Return True if credential itself is valid and reachable now.

        This signal should not mix quota depletion semantics.
        Quota/budget availability belongs to get_capacity_signal.
        """

    async def mark_success(self, credential: CredentialDict, meta: dict | None = None) -> None:
        """Notify plugin that a request completed successfully."""

    async def mark_error(self, credential: CredentialDict, error_meta: dict | None = None) -> None:
        """Notify plugin that a request failed."""

    async def explain_credential(self, credential: CredentialDict) -> dict:
        """Return a safe summary for admin display."""
        return {"provider": self.name, "status": "unknown"}

    async def get_capacity_signal(self, credential: CredentialDict) -> CapacitySignal | None:
        """Return normalized capacity and optional quota availability signal.

        Return None when provider does not have reliable quota data.
        """
        return None

    async def verify_upstream_root_reachable(self) -> None:
        """Probe supplier root URL without credentials before persisting new credentials.

        Default is a no-op (for tests or static-only plugins). Remote providers should override.
        """
        return

    async def prepare_credential(self, credential: CredentialDict) -> CredentialPreparationResult:
        """Return the locally normalized credential to persist.

        This hook is used by registration/update flows. Implementations must not perform
        remote IO, token refresh, runtime project discovery, or quota probing here.
        """
        return CredentialPreparationResult(credential=credential, changed=False)


class ProviderRegistry:
    """In-memory registry mapping provider names to plugin instances."""

    def __init__(self) -> None:
        self._plugins: dict[str, ProviderPlugin] = {}

    def register(self, plugin: ProviderPlugin) -> None:
        if plugin.PLUGIN_INTERFACE_VERSION != PLUGIN_INTERFACE_VERSION:
            raise ValueError(
                f"plugin {plugin.name} interface version mismatch: "
                f"{plugin.PLUGIN_INTERFACE_VERSION} != {PLUGIN_INTERFACE_VERSION}"
            )
        self._plugins[plugin.name.lower()] = plugin

    def get(self, name: str) -> ProviderPlugin | None:
        return self._plugins.get(name.lower())

    def all(self) -> list[ProviderPlugin]:
        return list(self._plugins.values())
