from __future__ import annotations

from abc import ABC, abstractmethod


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

    @property
    @abstractmethod
    def name(self) -> str:
        """Canonical provider identifier, e.g. 'openai'. Always lowercase."""

    @abstractmethod
    async def fetch_models(self, api_key: str) -> list[str]:
        """Return the list of model IDs available for this credential.

        Called once at credential registration / manual refresh.
        The result is stored in the account record for informational use.
        """

    @abstractmethod
    async def is_credential_available(self, api_key: str, model: str | None = None) -> bool:
        """Return True if the credential can currently handle a request.

        This is the ONLY availability signal the scheduling core uses.
        The plugin decides what "available" means — it may check balance,
        cookie validity, rate-limit windows, or any provider-specific rule.
        The core does NOT inspect the reason; it just skips unavailable
        credentials.
        """

    async def mark_success(self, api_key: str, meta: dict | None = None) -> None:
        """Notify the plugin that a request completed successfully.

        The plugin may update internal counters, reset cooldowns, etc.
        Default: no-op (stateless plugins do not need this).
        """

    async def mark_error(self, api_key: str, error_meta: dict | None = None) -> None:
        """Notify the plugin that a request failed.

        The plugin decides whether to enter cooldown, mark credential
        invalid, etc. Default: no-op.
        """

    async def explain_credential(self, api_key: str) -> dict:
        """Return a safe summary for admin display.

        Must NOT include the raw credential value or any sensitive field.
        """
        return {"provider": self.name, "status": "unknown"}


class ProviderRegistry:
    """In-memory registry mapping provider names to their plugin instances."""

    def __init__(self) -> None:
        self._plugins: dict[str, ProviderPlugin] = {}

    def register(self, plugin: ProviderPlugin) -> None:
        self._plugins[plugin.name.lower()] = plugin

    def get(self, name: str) -> ProviderPlugin | None:
        return self._plugins.get(name.lower())

    def all(self) -> list[ProviderPlugin]:
        return list(self._plugins.values())
