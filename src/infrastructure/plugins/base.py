from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

PLUGIN_INTERFACE_VERSION = "1.0.0"
ModelSource = Literal["remote", "static"]
CredentialDict = dict[str, str]


@dataclass(slots=True)
class CapacitySignal:
    has_capacity_signal: bool
    capacity_score: float | None
    capacity_kind: str
    reason: str


class ProviderPlugin(ABC):
    """Abstract base for all provider plugins.

    One plugin == one provider.

    The contract the core sees is minimal and strict:
        - fetch_models       : called once when a credential is registered
        - is_credential_available : the ONLY availability signal the core uses
        - mark_success / mark_error : outcome callbacks; plugin updates its
                                      own internal state (cooldown, etc.)
        - explain_credential : optional admin display — no sensitive fields

    Everything else (balance, pricing, quota, usage) is PRIVATE to the plugin.
    The core must NEVER call or depend on billing logic.
    """

    PLUGIN_VERSION = "1.0.0"
    PLUGIN_INTERFACE_VERSION = PLUGIN_INTERFACE_VERSION

    @property
    @abstractmethod
    def name(self) -> str:
        """Canonical provider identifier, e.g. 'openai'. Always lowercase."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of this provider and its credential."""

    @property
    @abstractmethod
    def auth_type(self) -> str:
        """Authentication mechanism, e.g. 'bearer_api_key', 'header_api_key', 'cookie'."""

    @property
    @abstractmethod
    def credential_hint(self) -> str:
        """Example / pattern of the credential, e.g. 'sk-...'."""

    @property
    def model_source(self) -> ModelSource:
        """Where fetch_models comes from: remote API or static table."""
        return "remote"

    def is_plugin_ready(self) -> bool:
        """Return True if the plugin's runtime dependencies are satisfied.

        Override in plugins that require optional third-party packages.
        The default implementation always returns True.
        """
        return True

    @abstractmethod
    async def fetch_models(self, credential: CredentialDict) -> list[str]:
        """Return the list of model IDs available for this credential.

        Called once at credential registration / manual refresh.
        The result is stored in the account record for informational use.
        """

    @abstractmethod
    async def is_credential_available(self, credential: CredentialDict, model: str | None = None) -> bool:
        """Return True if the credential can currently handle a request.

        This is the ONLY availability signal the scheduling core uses.
        The plugin decides what "available" means — it may check balance,
        cookie validity, rate-limit windows, or any provider-specific rule.
        The core does NOT inspect the reason; it just skips unavailable
        credentials.
        """

    async def mark_success(self, credential: CredentialDict, meta: dict | None = None) -> None:
        """Notify the plugin that a request completed successfully.

        The plugin may update internal counters, reset cooldowns, etc.
        Default: no-op (stateless plugins do not need this).
        """

    async def mark_error(self, credential: CredentialDict, error_meta: dict | None = None) -> None:
        """Notify the plugin that a request failed.

        The plugin decides whether to enter cooldown, mark credential
        invalid, etc. Default: no-op.
        """

    async def explain_credential(self, credential: CredentialDict) -> dict:
        """Return a safe summary for admin display.

        Must NOT include the raw credential value or any sensitive field.
        """
        return {"provider": self.name, "status": "unknown"}

    async def get_capacity_signal(self, credential: CredentialDict) -> CapacitySignal | None:
        """Return an optional normalized capacity signal for scheduling.

        Providers with no reliable balance/quota data should return None.
        """
        return None


class ProviderRegistry:
    """In-memory registry mapping provider names to their plugin instances."""

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
