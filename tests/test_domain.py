from datetime import datetime, timedelta, timezone

from domain.entities.api_key import ApiKey
from domain.services.scheduler import KeyScheduler
from domain.services.scorer import KeyScorer, ScoreWeights
from domain.services.state_machine import KeyStateMachine
from domain.value_objects.key_status import KeyStatus


def test_scorer_prefers_healthier_key() -> None:
    now = datetime.now(timezone.utc)
    scorer = KeyScorer(ScoreWeights())

    healthy = ApiKey(
        id="healthy",
        provider="openai",
        api_key="sk-1",
        success_count=20,
        error_count=1,
        last_used_at=now - timedelta(minutes=5),
    )
    weak = ApiKey(
        id="weak",
        provider="openai",
        api_key="sk-2",
        success_count=1,
        error_count=8,
        last_used_at=now - timedelta(seconds=5),
    )

    assert scorer.score(healthy, now) > scorer.score(weak, now)


def test_state_machine_applies_backoff_and_recovery() -> None:
    now = datetime.now(timezone.utc)
    key = ApiKey(id="k1", provider="openai", api_key="sk-test")
    machine = KeyStateMachine(rate_limit_backoff_minutes=(1, 2, 5))

    machine.on_error(key, "rate_limit", now)

    assert key.status == KeyStatus.RATE_LIMITED
    assert key.cooldown_until == now + timedelta(minutes=1)

    machine.recover_if_ready(key, now + timedelta(minutes=2))
    assert key.status == KeyStatus.AVAILABLE
    assert key.cooldown_until is None


def test_scheduler_selects_highest_ranked_key() -> None:
    now = datetime.now(timezone.utc)
    scheduler = KeyScheduler(KeyScorer(), jitter=0.0)

    better = ApiKey(
        id="better",
        provider="openai",
        api_key="sk-1",
        success_count=10,
        last_used_at=now - timedelta(minutes=10),
    )
    worse = ApiKey(
        id="worse",
        provider="openai",
        api_key="sk-2",
        error_count=5,
        last_used_at=now - timedelta(seconds=10),
    )

    selected = scheduler.select_key([worse, better], now)
    assert selected.id == "better"
