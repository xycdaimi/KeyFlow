"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-21
@Description: API Key 领域实体
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from domain.value_objects.key_status import KeyStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class ApiKey:
    """An API-key account.

    In KeyFlow the credential payload IS the account — one record represents
    one allocatable credential for a specific provider.

    Balance, quota, pricing, and billing state are PRIVATE to the provider
    plugin and are never stored here. The scheduler only uses the fields
    below plus the plugin's ``is_credential_available`` signal.
    """

    id: str
    provider: str
    credential: dict[str, str]
    """Provider-defined credential payload, e.g. {"api_key": "..."}."""

    status: KeyStatus = KeyStatus.AVAILABLE
    quota_used: int = 0
    """Token / request counter maintained by the core for scoring purposes."""
    last_used_at: datetime | None = None
    success_count: int = 0
    error_count: int = 0
    cooldown_until: datetime | None = None
    supported_models: list = field(default_factory=list)
    last_refreshed_at: datetime | None = None
    """Last time this key's availability/capacity was refreshed by the background task."""
    updated_at: datetime | None = None
    """Last time this key record was modified in storage."""
    cached_available: bool | None = None
    """Cached credential-level availability from plugin. None = not yet refreshed."""
    cached_quota_available: bool | None = None
    """Cached quota/budget availability from plugin signal. None = unknown."""
    cached_capacity_score: float | None = None
    """Cached capacity score from plugin. None = unknown or not refreshed."""
    """Model IDs fetched from the provider once at registration."""

    def idle_seconds(self, now: datetime | None = None) -> float:
        if self.last_used_at is None:
            return float("inf")
        current = now or utcnow()
        return max((current - self.last_used_at).total_seconds(), 0.0)

    def success_ratio(self) -> float:
        total = self.success_count + self.error_count
        if total == 0:
            return 1.0
        return self.success_count / total

    def is_available(self, now: datetime | None = None) -> bool:
        current = now or utcnow()
        if self.status in {
            KeyStatus.PENDING,
            KeyStatus.DISABLED_UPSTREAM,
            KeyStatus.DISABLED_ADMIN,
            KeyStatus.DISABLED_REPORT,
            KeyStatus.EXHAUSTED,
        }:
            return False
        if self.status in {KeyStatus.COOLDOWN, KeyStatus.RATE_LIMITED}:
            if self.cooldown_until:
                return self.cooldown_until <= current
            return False
        return True

    def mark_used(self, now: datetime | None = None) -> None:
        self.last_used_at = now or utcnow()

    def register_success(self, tokens_used: int = 0, now: datetime | None = None) -> None:
        self.success_count += 1
        self.quota_used += max(tokens_used, 0)
        self.last_used_at = now or utcnow()
        self.status = KeyStatus.AVAILABLE
        self.cooldown_until = None

    def register_error(self, now: datetime | None = None) -> None:
        self.error_count += 1
        self.last_used_at = now or utcnow()
