from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from domain.entities.api_key import ApiKey
from domain.exceptions.domain_exceptions import NoAvailableKeyError
from domain.services.scorer import KeyScorer


@dataclass(slots=True)
class RankedKey:
    key: ApiKey
    score: float


class KeyScheduler:
    def __init__(self, scorer: KeyScorer, jitter: float = 0.0, rng: random.Random | None = None) -> None:
        self._scorer = scorer
        self._jitter = max(jitter, 0.0)
        self._rng = rng or random.Random()

    def rank_keys(
        self,
        keys: list[ApiKey],
        now: datetime,
        capacity_by_key_id: dict[str, float | None] | None = None,
    ) -> list[RankedKey]:
        ranked: list[RankedKey] = []
        for key in keys:
            if not key.is_available(now):
                continue
            capacity_score = None if capacity_by_key_id is None else capacity_by_key_id.get(key.id)
            score = self._scorer.score(key, now, capacity_score=capacity_score)
            if score == float("-inf"):
                continue
            jitter = self._rng.uniform(0.0, self._jitter) if self._jitter else 0.0
            ranked.append(RankedKey(key=key, score=score + jitter))
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked

    def select_key(
        self,
        keys: list[ApiKey],
        now: datetime,
        capacity_by_key_id: dict[str, float | None] | None = None,
    ) -> ApiKey:
        ranked = self.rank_keys(keys, now, capacity_by_key_id=capacity_by_key_id)
        if not ranked:
            raise NoAvailableKeyError("no allocatable keys were found")
        return ranked[0].key
