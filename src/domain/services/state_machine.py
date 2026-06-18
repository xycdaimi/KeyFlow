"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-06-08
@Description: Key 状态机与上报驱动状态转换
"""
from __future__ import annotations

from datetime import datetime, timedelta

from domain.entities.api_key import ApiKey
from domain.value_objects.key_status import KeyStatus


class KeyStateMachine:
    _RATE_LIMIT_ERRORS = frozenset({"rate_limit", "too_many_requests", "429"})
    _FATAL_CREDENTIAL_ERRORS = frozenset(
        {
            "invalid_api_key",
            "unauthorized",
            "forbidden",
            "account_disabled",
            "credential_revoked",
            "oauth_refresh_failed",
        }
    )

    def __init__(
        self,
        rate_limit_backoff_minutes: tuple[int, ...] = (1, 2, 5, 10),
        report_transient_failure_threshold: int = 5,
        report_cooldown_disable_rounds: int = 3,
        report_backoff_minutes: tuple[int, ...] = (1, 2, 5, 10),
    ) -> None:
        self._rate_limit_backoff_minutes = rate_limit_backoff_minutes
        self._report_transient_failure_threshold = max(report_transient_failure_threshold, 1)
        self._report_cooldown_disable_rounds = max(report_cooldown_disable_rounds, 1)
        self._report_backoff_minutes = (
            tuple(max(int(item), 1) for item in report_backoff_minutes) or (1,)
        )

    def on_success(self, key: ApiKey, tokens_used: int, now: datetime) -> ApiKey:
        key.success_count += 1
        key.quota_used += max(tokens_used, 0)
        key.last_used_at = now
        return key

    def on_error(self, key: ApiKey, error_type: str, now: datetime) -> ApiKey:
        key.register_error(now=now)
        return key

    def on_report_success(self, key: ApiKey, tokens_used: int, now: datetime) -> ApiKey:
        key.success_count += 1
        key.quota_used += max(tokens_used, 0)
        key.last_used_at = now
        key.consecutive_error_count = 0
        key.rate_limit_rounds = 0
        return key

    def on_report_error(self, key: ApiKey, error_type: str, now: datetime) -> ApiKey:
        normalized = self.normalize_report_error_type(error_type)
        key.error_count += 1
        key.last_used_at = now
        key.last_report_error_type = normalized
        if key.status == KeyStatus.DISABLED_ADMIN:
            return key
        if normalized in self._FATAL_CREDENTIAL_ERRORS:
            key.status = KeyStatus.DISABLED_REPORT
            key.cooldown_until = None
            key.consecutive_error_count = 0
            return key
        if normalized in self._RATE_LIMIT_ERRORS:
            key.rate_limit_rounds += 1
            key.status = KeyStatus.RATE_LIMITED
            key.cooldown_until = now + timedelta(
                minutes=self._backoff_minutes(key.rate_limit_rounds)
            )
            return key

        key.consecutive_error_count += 1
        if key.consecutive_error_count < self._report_transient_failure_threshold:
            return key
        key.consecutive_error_count = 0
        key.cooldown_failure_rounds += 1
        if key.cooldown_failure_rounds >= self._report_cooldown_disable_rounds:
            key.status = KeyStatus.DISABLED_REPORT
            key.cooldown_until = None
            return key
        key.status = KeyStatus.COOLDOWN
        key.cooldown_until = now + timedelta(
            minutes=self._backoff_minutes(key.cooldown_failure_rounds)
        )
        return key

    def recover_if_ready(self, key: ApiKey, now: datetime) -> ApiKey:
        if key.status not in {KeyStatus.RATE_LIMITED, KeyStatus.COOLDOWN}:
            return key
        if key.cooldown_until and key.cooldown_until > now:
            return key
        key.status = KeyStatus.AVAILABLE
        key.cooldown_until = None
        return key

    @staticmethod
    def normalize_report_error_type(error_type: str) -> str:
        normalized = str(error_type or "").strip().lower().replace("-", "_").replace(" ", "_")
        return normalized or "execution_failed"

    def _backoff_minutes(self, round_count: int) -> int:
        index = min(max(round_count, 1), len(self._report_backoff_minutes)) - 1
        return self._report_backoff_minutes[index]
