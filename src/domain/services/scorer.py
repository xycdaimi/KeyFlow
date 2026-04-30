"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-21
@Description: Key 评分器与刷新时效惩罚
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from domain.entities.api_key import ApiKey
from domain.value_objects.key_status import KeyStatus


@dataclass(slots=True)
class ScoreWeights:
    capacity: float = 0.4
    idle: float = 0.35
    success: float = 0.35
    error: float = 0.2
    rate_limit: float = 0.05
    cooldown: float = 0.05
    capacity_unknown_fallback: float = 0.5
    freshness_stale_penalty: float = 0.15
    freshness_very_stale_penalty: float = 0.30
    freshness_stale_after_seconds: int = 60
    freshness_very_stale_after_seconds: int = 180
    idle_cap_seconds: int = 300
    error_cap: int = 10


class KeyScorer:
    def __init__(self, weights: ScoreWeights | None = None) -> None:
        self.weights = weights or ScoreWeights()

    def _freshness_penalty(self, key: ApiKey, now: datetime) -> float:
        if key.last_refreshed_at is None:
            return self.weights.freshness_very_stale_penalty

        age_seconds = max((now - key.last_refreshed_at).total_seconds(), 0.0)
        if age_seconds < self.weights.freshness_stale_after_seconds:
            return 0.0
        if age_seconds < self.weights.freshness_very_stale_after_seconds:
            return self.weights.freshness_stale_penalty
        return self.weights.freshness_very_stale_penalty

    def score(self, key: ApiKey, now: datetime, capacity_score: float | None = None) -> float:
        if key.status in {
            KeyStatus.PENDING,
            KeyStatus.DISABLED_UPSTREAM,
            KeyStatus.DISABLED_ADMIN,
            KeyStatus.DISABLED_REPORT,
        }:
            return float("-inf")
        if key.status == KeyStatus.EXHAUSTED:
            return float("-inf")

        idle_seconds = key.idle_seconds(now)
        idle_score = 1.0 if idle_seconds == float("inf") else min(
            idle_seconds / max(self.weights.idle_cap_seconds, 1),
            1.0,
        )
        success_score = key.success_ratio()
        error_penalty = min(key.error_count / max(self.weights.error_cap, 1), 1.0)
        rate_limit_penalty = 1.0 if key.status == KeyStatus.RATE_LIMITED else 0.0

        cooldown_penalty = 0.0
        if key.cooldown_until and key.cooldown_until > now:
            total = max(self.weights.idle_cap_seconds, 1)
            remaining = (key.cooldown_until - now).total_seconds()
            cooldown_penalty = min(remaining / total, 1.0)

        effective_capacity = (
            self.weights.capacity_unknown_fallback
            if capacity_score is None
            else min(max(capacity_score, 0.0), 1.0)
        )

        return (
            (self.weights.capacity * effective_capacity)
            + (self.weights.idle * idle_score)
            + (self.weights.success * success_score)
            - (self.weights.error * error_penalty)
            - (self.weights.rate_limit * rate_limit_penalty)
            - (self.weights.cooldown * cooldown_penalty)
            - self._freshness_penalty(key, now)
        )
