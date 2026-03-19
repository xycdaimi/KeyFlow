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
        credential={"api_key": "sk-1"},
        success_count=20,
        error_count=1,
        last_used_at=now - timedelta(minutes=5),
    )
    weak = ApiKey(
        id="weak",
        provider="openai",
        credential={"api_key": "sk-2"},
        success_count=1,
        error_count=8,
        last_used_at=now - timedelta(seconds=5),
    )

    assert scorer.score(healthy, now) > scorer.score(weak, now)


def test_state_machine_applies_backoff_and_recovery() -> None:
    now = datetime.now(timezone.utc)
    key = ApiKey(id="k1", provider="openai", credential={"api_key": "sk-test"})
    machine = KeyStateMachine(rate_limit_backoff_minutes=(1, 2, 5))

    machine.on_error(key, "rate_limit", now)

    assert key.status == KeyStatus.RATE_LIMITED
    assert key.cooldown_until == now + timedelta(minutes=1)

    machine.recover_if_ready(key, now + timedelta(minutes=2))
    assert key.status == KeyStatus.AVAILABLE
    assert key.cooldown_until is None


def test_state_machine_handles_generic_error_without_exhaustion_probe() -> None:
    now = datetime.now(timezone.utc)
    key = ApiKey(id="k-generic", provider="openai", credential={"api_key": "sk-test"})
    machine = KeyStateMachine()

    machine.on_error(key, "network_timeout", now)

    assert key.status == KeyStatus.AVAILABLE
    assert key.error_count == 1


def test_scheduler_selects_highest_ranked_key() -> None:
    now = datetime.now(timezone.utc)
    scheduler = KeyScheduler(KeyScorer(), jitter=0.0)

    better = ApiKey(
        id="better",
        provider="openai",
        credential={"api_key": "sk-1"},
        success_count=10,
        last_used_at=now - timedelta(minutes=10),
    )
    worse = ApiKey(
        id="worse",
        provider="openai",
        credential={"api_key": "sk-2"},
        error_count=5,
        last_used_at=now - timedelta(seconds=10),
    )

    selected = scheduler.select_key([worse, better], now)
    assert selected.id == "better"


def test_scorer_prefers_key_with_higher_capacity_signal() -> None:
    now = datetime.now(timezone.utc)
    scorer = KeyScorer(ScoreWeights(capacity=0.4))

    key_a = ApiKey(id="a", provider="openrouter", credential={"api_key": "a"})
    key_b = ApiKey(id="b", provider="openrouter", credential={"api_key": "b"})

    assert scorer.score(key_a, now, capacity_score=0.8) > scorer.score(
        key_b, now, capacity_score=0.2
    )


def test_scorer_uses_neutral_fallback_when_capacity_missing() -> None:
    now = datetime.now(timezone.utc)
    scorer = KeyScorer(ScoreWeights(capacity=0.4, capacity_unknown_fallback=0.5))
    key = ApiKey(id="fallback", provider="openai", credential={"api_key": "sk-test"})

    assert scorer.score(key, now, capacity_score=None) == scorer.score(key, now, capacity_score=0.5)


def test_scheduler_uses_capacity_signal_as_runtime_tiebreaker() -> None:
    now = datetime.now(timezone.utc)
    scheduler = KeyScheduler(KeyScorer(ScoreWeights(capacity=0.4)), jitter=0.0)

    low = ApiKey(id="low", provider="openrouter", credential={"api_key": "low"})
    high = ApiKey(id="high", provider="openrouter", credential={"api_key": "high"})

    selected = scheduler.select_key(
        [low, high],
        now,
        capacity_by_key_id={"low": 0.1, "high": 0.9},
    )

    assert selected.id == "high"
