"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-03
@Description: 供应商插件抽象基类与注册表
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

import httpx

from domain.exceptions.domain_exceptions import UpstreamUnreachableError

PLUGIN_INTERFACE_VERSION = "1.0.0"
ModelSource = Literal["remote", "static"]
CredentialDict = dict[str, str]

_UPSTREAM_ROOT_HTTP_TIMEOUT = httpx.Timeout(5.0)


async def ensure_upstream_root_http_reachable(origin: str) -> None:
    """GET ``{origin}/`` with no auth. Any HTTP response counts as reachable."""
    root = origin.rstrip("/") + "/"
    try:
        async with httpx.AsyncClient(
            timeout=_UPSTREAM_ROOT_HTTP_TIMEOUT,
            follow_redirects=True,
        ) as client:
            await client.get(root)
    except httpx.RequestError as exc:
        raise UpstreamUnreachableError(root) from exc


@dataclass(slots=True)
class CapacitySignal:
    has_capacity_signal: bool
    capacity_score: float | None
    quota_available: bool | None = None
    capacity_kind: str = "unknown"
    reason: str = ""


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
        """Where fetch_models comes from: remote API or static table."""
        return "remote"

    def is_plugin_ready(self) -> bool:
        """Return True if plugin runtime dependencies are satisfied."""
        return True

    @abstractmethod
    async def fetch_models(self, credential: CredentialDict) -> list[str]:
        """Return model IDs available for this credential."""

    @abstractmethod
    async def is_credential_available(self, credential: CredentialDict, model: str | None = None) -> bool:
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
