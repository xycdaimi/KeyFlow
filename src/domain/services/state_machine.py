from __future__ import annotations

from datetime import datetime, timedelta

from domain.entities.api_key import ApiKey
from domain.value_objects.key_status import KeyStatus


class KeyStateMachine:
    def __init__(self, rate_limit_backoff_minutes: tuple[int, ...] = (1, 2, 5, 10)) -> None:
        self._rate_limit_backoff_minutes = rate_limit_backoff_minutes

    def on_success(self, key: ApiKey, tokens_used: int, now: datetime) -> ApiKey:
        if key.status == KeyStatus.DISABLED_ADMIN:
            return key
        key.register_success(tokens_used=tokens_used, now=now)
        return key

    def on_error(self, key: ApiKey, error_type: str, now: datetime) -> ApiKey:
        normalized = error_type.strip().lower()
        key.register_error(now=now)

        if normalized == "rate_limit":
            step = min(max(key.error_count - 1, 0), len(self._rate_limit_backoff_minutes) - 1)
            key.status = KeyStatus.RATE_LIMITED
            key.cooldown_until = now + timedelta(minutes=self._rate_limit_backoff_minutes[step])
            return key

        if normalized == "quota_exhausted":
            key.status = KeyStatus.EXHAUSTED
            return key

        if normalized == "disabled":
            key.status = KeyStatus.DISABLED_REPORT
            return key

        key.status = KeyStatus.AVAILABLE
        return key

    def recover_if_ready(self, key: ApiKey, now: datetime) -> ApiKey:
        if key.status not in {KeyStatus.RATE_LIMITED, KeyStatus.COOLDOWN}:
            return key
        if key.cooldown_until and key.cooldown_until > now:
            key.status = KeyStatus.COOLDOWN
            return key
        key.status = KeyStatus.AVAILABLE
        key.cooldown_until = None
        return key
