# Full Candidate Allocation Freshness Penalty Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure every credential that supports the requested model and is in a strictly allocatable state participates in ranking, while cache freshness only affects score and never candidate admission.

**Architecture:** Allocation keeps three layers with strict boundaries. `KeyService` builds the full candidate set using only model support and allocatable status, and `_is_cache_fresh()` is removed from the allocation path entirely. `KeyScorer` computes the final score, including a fixed three-band freshness penalty based on `last_refreshed_at`; those scoring thresholds live only inside scorer weights and are not shared with refresh scheduling. Redis lease logic does not provide a cross-provider global atomic "pick rank #1" guarantee. The actual behavior stays aligned with the current implementation: provider-scoped allocation passes one ranked provider list into a single provider Lua lease attempt, while cross-provider allocation walks the globally ranked list in order and tries provider-local lease acquisition one candidate at a time until one succeeds.

**Tech Stack:** Python 3, pytest, FastAPI test client, in-memory repository/allocation fakes, Redis Lua allocation store

---

## File Structure

- Modify: `src/application/services/key_service.py`
  - Remove `_is_cache_fresh()` and stop filtering candidates by refresh age.
- Modify: `src/domain/services/scorer.py`
  - Add fixed-band freshness scoring inside `KeyScorer.score()` without changing its public signature.
- Modify: `tests/test_domain.py`
  - Add domain-level regression tests for stale participation, stale-only fallback, fresh-over-stale ordering, and lease fallback.
- Modify: `tests/test_api.py`
  - Add route-level regressions to prove stale supported keys still allocate and fresh keys still outrank equivalent stale keys.

### Task 1: Lock the candidate-set behavior with failing domain tests

**Files:**
- Modify: `tests/test_domain.py`
- Test: `tests/test_domain.py`

- [ ] **Step 1: Write the failing provider-scoped allocation test for stale candidate participation**

```python
@pytest.mark.anyio
async def test_allocate_key_includes_stale_supported_key_in_ranked_candidates() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="fresh-key",
                provider="openai",
                credential={"api_key": "sk-fresh"},
                supported_models=["gpt-4o"],
                last_used_at=now - timedelta(minutes=5),
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=0.6,
            ),
            ApiKey(
                id="stale-key",
                provider="openai",
                credential={"api_key": "sk-stale"},
                supported_models=["gpt-4o"],
                last_used_at=now - timedelta(minutes=5),
                last_refreshed_at=now - timedelta(seconds=120),
                cached_available=True,
                cached_capacity_score=0.6,
            ),
        ]
    )
    allocation_store = InMemoryAllocationStore()
    scorer = KeyScorer()
    scheduler = KeyScheduler(scorer, jitter=0.0)
    service = KeyService(
        repository,
        allocation_store,
        scheduler,
        scorer,
        KeyStateMachine(),
        build_provider_registry(FakeProviderPlugin("openai", ["gpt-4o"], available=True)),
        refresh_cache_seconds=60,
    )

    await service.allocate_key("openai", "gpt-4o")

    assert allocation_store.allocate_calls == [
        ("openai", ["fresh-key", "stale-key"]),
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_domain.py::test_allocate_key_includes_stale_supported_key_in_ranked_candidates -v`

Expected: FAIL because the current `_collect_candidates()` drops `stale-key` before ranking.

- [ ] **Step 3: Write the failing cross-provider stale-only fallback test**

```python
@pytest.mark.anyio
async def test_allocate_by_model_allows_stale_key_when_it_is_the_only_allocatable_candidate() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="disabled-openai",
                provider="openai",
                credential={"api_key": "sk-disabled"},
                status=KeyStatus.DISABLED_ADMIN,
                supported_models=["gpt-4o"],
                last_refreshed_at=now,
                cached_available=True,
            ),
            ApiKey(
                id="stale-openrouter",
                provider="openrouter",
                credential={"api_key": "sk-stale"},
                status=KeyStatus.AVAILABLE,
                supported_models=["gpt-4o"],
                last_refreshed_at=now - timedelta(seconds=120),
                cached_available=True,
                cached_capacity_score=0.4,
            ),
        ]
    )
    scorer = KeyScorer()
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(scorer, jitter=0.0),
        scorer,
        KeyStateMachine(),
        build_provider_registry(
            FakeProviderPlugin("openai", ["gpt-4o"], available=True),
            FakeProviderPlugin("openrouter", ["gpt-4o"], available=True),
        ),
        refresh_cache_seconds=60,
    )

    selected = await service.allocate_key_by_model("gpt-4o")

    assert selected.key.id == "stale-openrouter"
    assert selected.provider_model == "gpt-4o"
```

- [ ] **Step 4: Run test to verify it fails**

Run: `python -m pytest tests/test_domain.py::test_allocate_by_model_allows_stale_key_when_it_is_the_only_allocatable_candidate -v`

Expected: FAIL with `NoAvailableKeyError` under the current freshness filter.

- [ ] **Step 5: Write the failing lease fallback test**

```python
@pytest.mark.anyio
async def test_allocate_key_tries_next_ranked_candidate_when_first_is_leased() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="fresh-first",
                provider="openai",
                credential={"api_key": "sk-first"},
                supported_models=["gpt-4o"],
                last_used_at=now - timedelta(minutes=5),
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=0.8,
            ),
            ApiKey(
                id="stale-second",
                provider="openai",
                credential={"api_key": "sk-second"},
                supported_models=["gpt-4o"],
                last_used_at=now - timedelta(minutes=5),
                last_refreshed_at=now - timedelta(seconds=120),
                cached_available=True,
                cached_capacity_score=0.7,
            ),
        ]
    )
    allocation_store = InMemoryAllocationStore()
    allocation_store.active_leases[("openai", "fresh-first")] = now + timedelta(seconds=10)
    scorer = KeyScorer()
    service = KeyService(
        repository,
        allocation_store,
        KeyScheduler(scorer, jitter=0.0),
        scorer,
        KeyStateMachine(),
        build_provider_registry(FakeProviderPlugin("openai", ["gpt-4o"], available=True)),
        refresh_cache_seconds=60,
    )

    selected = await service.allocate_key("openai", "gpt-4o")

    assert selected.key.id == "stale-second"
```

- [ ] **Step 6: Run test to verify it fails**

Run: `python -m pytest tests/test_domain.py::test_allocate_key_tries_next_ranked_candidate_when_first_is_leased -v`

Expected: FAIL because the current candidate filter removes `stale-second`, leaving no fallback candidate.

- [ ] **Step 7: Commit the red test phase**

```bash
git add tests/test_domain.py
git commit -m "test: cover stale candidate allocation behavior"
```

### Task 2: Lock the freshness scoring behavior with failing scorer tests

**Files:**
- Modify: `tests/test_domain.py`
- Test: `tests/test_domain.py`

- [ ] **Step 1: Write the failing fresh-over-stale scoring test**

```python
def test_scheduler_prefers_fresh_key_over_equivalent_stale_key() -> None:
    now = datetime.now(timezone.utc)
    scorer = KeyScorer()
    scheduler = KeyScheduler(scorer, jitter=0.0)
    fresh = ApiKey(
        id="fresh",
        provider="openai",
        credential={"api_key": "sk-fresh"},
        last_used_at=now - timedelta(minutes=5),
        last_refreshed_at=now,
    )
    stale = ApiKey(
        id="stale",
        provider="openai",
        credential={"api_key": "sk-stale"},
        last_used_at=now - timedelta(minutes=5),
        last_refreshed_at=now - timedelta(seconds=120),
    )

    ranked = scheduler.rank_keys(
        [stale, fresh],
        now,
        capacity_by_key_id={"fresh": 0.7, "stale": 0.7},
    )

    assert [item.key.id for item in ranked] == ["fresh", "stale"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_domain.py::test_scheduler_prefers_fresh_key_over_equivalent_stale_key -v`

Expected: FAIL because `KeyScorer.score()` currently ignores refresh age.

- [ ] **Step 3: Write the failing stale-vs-very-stale scoring test**

```python
def test_scheduler_prefers_stale_key_over_very_stale_key_when_other_signals_match() -> None:
    now = datetime.now(timezone.utc)
    scorer = KeyScorer()
    scheduler = KeyScheduler(scorer, jitter=0.0)
    stale = ApiKey(
        id="stale",
        provider="openai",
        credential={"api_key": "sk-stale"},
        last_used_at=now - timedelta(minutes=5),
        last_refreshed_at=now - timedelta(seconds=120),
    )
    very_stale = ApiKey(
        id="very-stale",
        provider="openai",
        credential={"api_key": "sk-very-stale"},
        last_used_at=now - timedelta(minutes=5),
        last_refreshed_at=now - timedelta(seconds=240),
    )

    ranked = scheduler.rank_keys(
        [very_stale, stale],
        now,
        capacity_by_key_id={"stale": 0.7, "very-stale": 0.7},
    )

    assert [item.key.id for item in ranked] == ["stale", "very-stale"]
```

- [ ] **Step 4: Run test to verify it fails**

Run: `python -m pytest tests/test_domain.py::test_scheduler_prefers_stale_key_over_very_stale_key_when_other_signals_match -v`

Expected: FAIL because the scorer does not yet distinguish stale from very stale.

- [ ] **Step 5: Commit the freshness scoring red phase**

```bash
git add tests/test_domain.py
git commit -m "test: cover freshness penalty scoring"
```

### Task 3: Remove freshness filtering from candidate collection

**Files:**
- Modify: `src/application/services/key_service.py`
- Test: `tests/test_domain.py`

- [ ] **Step 1: Delete `_is_cache_fresh()` from `KeyService`**

```python
-    def _is_cache_fresh(self, key: ApiKey, now: datetime) -> bool:
-        """True if key has fresh cached availability/capacity (no plugin calls needed)."""
-        if key.last_refreshed_at is None:
-            return False
-        return (now - key.last_refreshed_at).total_seconds() < self._refresh_cache_seconds
```

- [ ] **Step 2: Remove freshness-based candidate filtering in `_collect_candidates()`**

```python
async def _collect_candidates(
    self,
    keys: list[ApiKey],
    model: str | None,
    now: datetime,
) -> tuple[list[ApiKey], dict[str, float | None], dict[str, str | None]]:
    candidates: list[ApiKey] = []
    capacity_by_key_id: dict[str, float | None] = {}
    provider_model_by_key_id: dict[str, str | None] = {}

    for key in keys:
        if not key.is_available(now):
            continue

        provider_model: str | None = None
        if model:
            provider_model = self._model_alias_resolver.resolve_provider_model(
                requested_model=model,
                provider=key.provider,
                supported_models=list(key.supported_models),
            )
            if provider_model is None:
                continue

        plugin = self._provider_registry.get(key.provider)
        capacity_by_key_id[key.id] = key.cached_capacity_score if plugin is not None else None
        provider_model_by_key_id[key.id] = provider_model
        candidates.append(key)

    return candidates, capacity_by_key_id, provider_model_by_key_id
```

- [ ] **Step 3: Run the candidate-set regression tests**

Run: `python -m pytest tests/test_domain.py::test_allocate_key_includes_stale_supported_key_in_ranked_candidates tests/test_domain.py::test_allocate_by_model_allows_stale_key_when_it_is_the_only_allocatable_candidate -v`

Expected:
- `tests/test_domain.py::test_allocate_key_includes_stale_supported_key_in_ranked_candidates` MUST PASS.
- `tests/test_domain.py::test_allocate_by_model_allows_stale_key_when_it_is_the_only_allocatable_candidate` MUST PASS.
- Any failure is a stop condition for this task.
- Do not run the lease fallback test in this task. Lease fallback is a ranking-dependent assertion and belongs to Task 4 after freshness scoring is implemented.

- [ ] **Step 4: Commit the service candidate-set change**

```bash
git add src/application/services/key_service.py tests/test_domain.py
git commit -m "feat: include stale allocatable keys in candidates"
```

### Task 4: Add fixed-band freshness scoring inside `KeyScorer`

**Files:**
- Modify: `src/domain/services/scorer.py`
- Test: `tests/test_domain.py`

This task owns ranking behavior. Candidate admission is already settled in Task 3. The lease fallback test is intentionally validated here, not earlier, because it depends on the final ranked order.
Interface constraint:
- `KeyScorer(...)` constructor signature remains unchanged.
- `KeyScorer.score(key, now, capacity_score=None)` signature remains unchanged.
- This task must not introduce any call-site modification work in production code.

- [ ] **Step 1: Add freshness penalty weights to `ScoreWeights`**

```python
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
```

- [ ] **Step 2: Add a private freshness penalty helper inside `KeyScorer`**

```python
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
```

- [ ] **Step 3: Apply freshness penalty inside `score()` without changing the public signature**

```python
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
        - self._freshness_penalty(key, now)
    )
```

- [ ] **Step 4: Run the scoring and ranking regressions**

Context:
- `tests/test_domain.py::test_scheduler_selects_highest_ranked_key` is an existing regression test in the repository.
- It is included here to prove that freshness scoring does not break the existing ranking behavior driven by success/error/idle inputs when freshness signals are equal.

Run: `python -m pytest tests/test_domain.py::test_scheduler_prefers_fresh_key_over_equivalent_stale_key tests/test_domain.py::test_scheduler_prefers_stale_key_over_very_stale_key_when_other_signals_match tests/test_domain.py::test_scheduler_selects_highest_ranked_key tests/test_domain.py::test_allocate_key_tries_next_ranked_candidate_when_first_is_leased -v`

Expected:
- `tests/test_domain.py::test_scheduler_prefers_fresh_key_over_equivalent_stale_key` MUST PASS.
- `tests/test_domain.py::test_scheduler_prefers_stale_key_over_very_stale_key_when_other_signals_match` MUST PASS.
- `tests/test_domain.py::test_scheduler_selects_highest_ranked_key` MUST PASS.
- `tests/test_domain.py::test_allocate_key_tries_next_ranked_candidate_when_first_is_leased` MUST PASS.
- Any failure is a stop condition for this task.

- [ ] **Step 5: Commit the scorer change**

```bash
git add src/domain/services/scorer.py tests/test_domain.py
git commit -m "feat: score freshness inside key scorer"
```

### Task 5: Add API regressions for stale candidate allocation

**Files:**
- Modify: `tests/test_api.py`
- Test: `tests/test_api.py`

Boundary:
- These API regressions validate allocation behavior under the current implementation's lease semantics.
- They verify ordered candidate selection plus provider-local lease attempts.
- They do not claim or test a cross-provider global atomic "always pick the absolute rank #1 key" guarantee.

- [ ] **Step 1: Add a stale-only route fallback test**

```python
def test_allocate_key_route_allows_stale_supported_key_when_it_is_only_provider_candidate() -> None:
    now = datetime.now(timezone.utc)
    client = build_test_client(
        repository=InMemoryKeyRepository(
            [
                ApiKey(
                    id="stale-only",
                    provider="openai",
                    credential={"api_key": "sk-stale"},
                    supported_models=["gpt-4o"],
                    status=KeyStatus.AVAILABLE,
                    last_refreshed_at=now - timedelta(seconds=120),
                    cached_available=True,
                    cached_capacity_score=0.3,
                ),
                ApiKey(
                    id="disabled-other",
                    provider="openai",
                    credential={"api_key": "sk-disabled"},
                    supported_models=["gpt-4o"],
                    status=KeyStatus.DISABLED_ADMIN,
                    last_refreshed_at=now,
                    cached_available=True,
                    cached_capacity_score=0.9,
                ),
            ]
        ),
        plugins=[FakeProviderPlugin("openai", ["gpt-4o"], available=True)],
    )

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai", "model": "gpt-4o"},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["key_id"] == "stale-only"
```

```python
def test_allocate_by_model_route_allows_stale_supported_key_when_it_is_only_candidate() -> None:
    now = datetime.now(timezone.utc)
    client = build_cross_provider_client(
        keys=[
            ApiKey(
                id="stale-only",
                provider="openai",
                credential={"api_key": "sk-stale"},
                supported_models=["gpt-4o"],
                status=KeyStatus.AVAILABLE,
                last_refreshed_at=now - timedelta(seconds=120),
                cached_available=True,
                cached_capacity_score=0.3,
            ),
            ApiKey(
                id="disabled-other",
                provider="openrouter",
                credential={"api_key": "sk-disabled"},
                supported_models=["gpt-4o"],
                status=KeyStatus.DISABLED_ADMIN,
                last_refreshed_at=now,
                cached_available=True,
                cached_capacity_score=0.9,
            ),
        ],
        plugins=[
            FakeProviderPlugin("openai", ["gpt-4o"], available=True),
            FakeProviderPlugin("openrouter", ["gpt-4o"], available=True),
        ],
    )

    response = client.post(
        "/api/internal/allocate-by-model",
        json={"model": "gpt-4o"},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["key_id"] == "stale-only"
```

- [ ] **Step 2: Add a route-level fresh-over-stale ordering regression**

```python
def test_allocate_by_model_route_prefers_fresh_candidate_over_equivalent_stale_candidate() -> None:
    now = datetime.now(timezone.utc)
    allocation_store = InMemoryAllocationStore()
    client = build_test_client(
        repository=InMemoryKeyRepository(
            [
                ApiKey(
                    id="fresh-key",
                    provider="openai",
                    credential={"api_key": "sk-fresh"},
                    supported_models=["gpt-4o"],
                    last_used_at=now - timedelta(minutes=5),
                    last_refreshed_at=now,
                    cached_available=True,
                    cached_capacity_score=0.5,
                ),
                ApiKey(
                    id="stale-key",
                    provider="openai",
                    credential={"api_key": "sk-stale"},
                    supported_models=["gpt-4o"],
                    last_used_at=now - timedelta(minutes=5),
                    last_refreshed_at=now - timedelta(seconds=120),
                    cached_available=True,
                    cached_capacity_score=0.5,
                ),
            ]
        ),
        plugins=[FakeProviderPlugin("openai", ["gpt-4o"], available=True)],
        allocation_store=allocation_store,
    )

    response = client.post(
        "/api/internal/allocate-by-model",
        json={"model": "gpt-4o"},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["key_id"] == "fresh-key"
    assert client.app.state.test_allocation_store.any_provider_ordered_ids == ["fresh-key", "stale-key"]
```

- [ ] **Step 3: Run the focused API regressions**

Run: `python -m pytest tests/test_api.py::test_allocate_key_route_allows_stale_supported_key_when_it_is_only_provider_candidate tests/test_api.py::test_allocate_by_model_route_allows_stale_supported_key_when_it_is_only_candidate tests/test_api.py::test_allocate_by_model_route_prefers_fresh_candidate_over_equivalent_stale_candidate tests/test_api.py::test_allocate_by_model_returns_provider_and_credential -v`

Expected:
- `tests/test_api.py::test_allocate_key_route_allows_stale_supported_key_when_it_is_only_provider_candidate` MUST PASS.
- `tests/test_api.py::test_allocate_by_model_route_allows_stale_supported_key_when_it_is_only_candidate` MUST PASS.
- `tests/test_api.py::test_allocate_by_model_route_prefers_fresh_candidate_over_equivalent_stale_candidate` MUST PASS.
- `tests/test_api.py::test_allocate_by_model_returns_provider_and_credential` MUST PASS.
- Any failure is a stop condition for this task.

- [ ] **Step 4: Run the final focused suite**

Run: `python -m pytest tests/test_domain.py tests/test_api.py -k "stale or freshness or allocate_by_model or allocate_key" -v`

Expected: PASS with the new stale-candidate and freshness-ranking regressions included.

- [ ] **Step 5: Commit the API regression coverage**

```bash
git add tests/test_api.py tests/test_domain.py
git commit -m "test: cover stale candidate allocation routes"
```

## Self-Review

- Spec coverage checked:
  - Full candidate participation is covered by Task 1 and Task 3.
  - Freshness as a scoring-only concern is covered by Task 2 and Task 4.
  - Strict allocatable status gating remains in `key.is_available(now)` and is preserved by Task 3.
  - Redis lease remains a claiming layer and is covered by the lease fallback regression in Task 4.
  - API route behavior is covered by Task 5.
- Placeholder scan checked:
  - No `TODO`, `TBD`, or deferred implementation phrases remain.
  - Every task includes exact files, commands, and expected outcomes.
- Type and boundary consistency checked:
  - `KeyService` no longer computes freshness penalty anywhere in the plan.
  - `KeyScheduler.rank_keys()` and `KeyScheduler.select_key()` keep their existing public signatures.
  - `KeyScorer.score()` keeps its existing public signature and owns freshness scoring internally.
  - Freshness scoring thresholds exist only in `ScoreWeights`; refresh scheduling keeps using `KeyService.refresh_cache_seconds` for background refresh work.
  - Provider-scoped and cross-provider tests are named and exercised consistently.
