from __future__ import annotations

from datetime import datetime

from domain.entities.api_key import ApiKey
from domain.value_objects.key_status import KeyStatus


class KeyStateMachine:
    def __init__(self, rate_limit_backoff_minutes: tuple[int, ...] = (1, 2, 5, 10)) -> None:
        self._rate_limit_backoff_minutes = rate_limit_backoff_minutes

    def on_success(self, key: ApiKey, tokens_used: int, now: datetime) -> ApiKey:
        key.success_count += 1
        key.quota_used += max(tokens_used, 0)
        key.last_used_at = now
        return key

    def on_error(self, key: ApiKey, error_type: str, now: datetime) -> ApiKey:
        key.register_error(now=now)
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
