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
    idle_cap_seconds: int = 300
    error_cap: int = 10


class KeyScorer:
    def __init__(self, weights: ScoreWeights | None = None) -> None:
        self.weights = weights or ScoreWeights()

    def score(self, key: ApiKey, now: datetime, capacity_score: float | None = None) -> float:
        if key.status in {
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
        )
