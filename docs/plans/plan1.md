# Cross-Provider Model Allocation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为账户池新增“只传模型名、跨多个 provider 统一择优分配 key”的第二条链路，并向上游返回凭证及对应 provider。

**Architecture:** 保留现有 `POST /api/internal/allocate-key` 作为第一条链路，不改变其“按 provider 分配”的职责；新增一条独立内部接口处理第二条链路。应用层新增跨 provider 分配方法，复用现有评分器与状态机，但扩展分配存储接口以支持跨 provider 租约抢占，避免并发下重复分配。文档与测试同步更新，优先以 TDD 方式逐步交付。

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy repository abstraction, Redis allocation cache with Lua script, pytest, punq

---

### Task 1: 定义第二条链路的 API 契约

**Files:**
- Modify: `src/interfaces/schemas/request.py`
- Modify: `src/interfaces/schemas/response.py`
- Test: `tests/test_api.py`

**Step 1: Write the failing test**

在 `tests/test_api.py` 新增一个独立的接口契约测试，验证新接口只接收 `model`，并在成功时返回 `status`、`key_id`、`provider`、`credential`。

```python
def test_allocate_by_model_returns_provider_and_credential() -> None:
    client = build_cross_provider_client()

    response = client.post(
        "/api/internal/allocate-by-model",
        json={"model": "gpt-4o"},
        headers={"X-Internal-Key": "test-key"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "key_id": "key-openrouter",
        "provider": "openrouter",
        "credential": {"api_key": "sk-or-best"},
    }
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_api.py::test_allocate_by_model_returns_provider_and_credential -v`
Expected: FAIL with `404 Not Found` or response schema mismatch because `/api/internal/allocate-by-model` does not exist yet.

**Step 3: Write minimal implementation**

先只补齐 schema，让实现目标明确：

```python
class AllocateByModelRequest(BaseModel):
    model: str


class AllocateByModelResponse(BaseModel):
    status: str
    key_id: str
    provider: str
    credential: dict[str, str]
```

保留现有 `AllocateRequest` / `AllocateResponse` 不动，避免影响第一条链路。

**Step 4: Run test to verify it still fails for the right reason**

Run: `pytest tests/test_api.py::test_allocate_by_model_returns_provider_and_credential -v`
Expected: FAIL, but failure should now focus on missing route / missing service method instead of missing schema types.

**Step 5: Commit**

```bash
git add tests/test_api.py src/interfaces/schemas/request.py src/interfaces/schemas/response.py
git commit -m "test: define contract for cross-provider allocation API"
```

### Task 2: 增加新内部路由并接入服务层

**Files:**
- Modify: `src/interfaces/api/routes/allocate.py`
- Modify: `src/interfaces/schemas/request.py`
- Modify: `src/interfaces/schemas/response.py`
- Test: `tests/test_api.py`

**Step 1: Write the failing test**

在 `tests/test_api.py` 新增两个接口行为测试：成功分配与无可用 key 返回 404。

```python
def test_allocate_by_model_returns_404_when_no_candidate_exists() -> None:
    client = build_cross_provider_client(all_available=False)

    response = client.post(
        "/api/internal/allocate-by-model",
        json={"model": "gpt-4o"},
        headers={"X-Internal-Key": "test-key"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "no_available_key"}
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_api.py::test_allocate_by_model_returns_provider_and_credential tests/test_api.py::test_allocate_by_model_returns_404_when_no_candidate_exists -v`
Expected: FAIL because route handler is missing.

**Step 3: Write minimal implementation**

在 `src/interfaces/api/routes/allocate.py` 增加新路由，沿用现有内部鉴权与异常映射：

```python
@router.post("/allocate-by-model", response_model=AllocateByModelResponse)
async def allocate_by_model(
    payload: AllocateByModelRequest,
    x_internal_key: Annotated[str | None, Header()] = None,
    service: KeyService = Depends(get_key_service),
    settings: Settings = Depends(get_settings),
) -> AllocateByModelResponse:
    await verify_internal_key(settings, x_internal_key)
    try:
        key = await service.allocate_key_by_model(payload.model)
    except NoAvailableKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no_available_key",
        ) from exc

    return AllocateByModelResponse(
        status="ok",
        key_id=key.id,
        provider=key.provider,
        credential=key.credential,
    )
```

这里只接线，不在路由里写筛选或评分逻辑。

**Step 4: Run test to verify it passes or fails one layer deeper**

Run: `pytest tests/test_api.py::test_allocate_by_model_returns_provider_and_credential tests/test_api.py::test_allocate_by_model_returns_404_when_no_candidate_exists -v`
Expected: FAIL due to missing `KeyService.allocate_key_by_model()`; route registration and auth path should now be correct.

**Step 5: Commit**

```bash
git add tests/test_api.py src/interfaces/api/routes/allocate.py src/interfaces/schemas/request.py src/interfaces/schemas/response.py
git commit -m "feat: add route for cross-provider model allocation"
```

### Task 3: 为服务层新增跨 provider 候选筛选与评分逻辑

**Files:**
- Modify: `src/application/services/key_service.py`
- Test: `tests/test_api.py`
- Test: `tests/test_domain.py`
- Test: `tests/fakes.py`

**Step 1: Write the failing test**

优先写服务行为测试，验证多个 provider 的候选 key 会按统一分数选出最佳 key，而不是限制在单一 provider 内。

```python
def test_allocate_by_model_selects_best_key_across_providers() -> None:
    now = datetime.now(timezone.utc)
    repository = InMemoryKeyRepository(
        [
            ApiKey(
                id="key-openai",
                provider="openai",
                credential={"api_key": "sk-openai"},
                last_used_at=now - timedelta(seconds=30),
            ),
            ApiKey(
                id="key-openrouter",
                provider="openrouter",
                credential={"api_key": "sk-or-best"},
                last_used_at=now - timedelta(minutes=5),
            ),
        ]
    )
    provider_registry = build_provider_registry(
        FakeProviderPlugin("openai", ["gpt-4o"], available=True),
        FakeProviderPlugin("openrouter", ["gpt-4o"], available=True),
    )
    service = KeyService(
        repository,
        InMemoryAllocationStore(),
        KeyScheduler(KeyScorer(), jitter=0.0),
        KeyScorer(),
        KeyStateMachine(),
        provider_registry,
    )

    selected = anyio.run(service.allocate_key_by_model, "gpt-4o")

    assert selected.id == "key-openrouter"
    assert selected.provider == "openrouter"
```

再补一条测试验证不支持模型的 key 会被排除：

```python
def test_allocate_by_model_filters_candidates_by_supported_models() -> None:
    ...
    assert selected.id == "key-match"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_api.py -k "allocate_by_model" -v`
Expected: FAIL with `AttributeError: 'KeyService' object has no attribute 'allocate_key_by_model'`.

**Step 3: Write minimal implementation**

在 `KeyService` 中新增一个只负责跨 provider 候选收集与排序的方法：

```python
async def allocate_key_by_model(self, model: str) -> ApiKey:
    now = utcnow()
    keys = await self._repository.list_keys()
    candidates: list[ApiKey] = []
    capacity_by_key_id: dict[str, float | None] = {}

    for key in keys:
        self._state_machine.recover_if_ready(key, now)
        if key.status == KeyStatus.AVAILABLE:
            await self._repository.upsert_key(key)
        if not key.is_available(now):
            continue
        if key.supported_models and model not in key.supported_models:
            continue

        plugin = self._provider_registry.get(key.provider)
        if plugin is not None:
            available = await plugin.is_credential_available(key.credential, model)
            if not available:
                continue
            signal = await plugin.get_capacity_signal(key.credential)
            capacity_by_key_id[key.id] = None if signal is None else signal.capacity_score
        else:
            capacity_by_key_id[key.id] = None

        candidates.append(key)

    ranked = self._scheduler.rank_keys(candidates, now, capacity_by_key_id=capacity_by_key_id)
    if not ranked:
        raise NoAvailableKeyError("no available key")

    allocated = await self._allocation_store.allocate_key_any_provider(ranked, now, self._allocation_lease_seconds)
    ...
```

注意事项：
- 提取公共候选筛选逻辑为私有 helper，例如 `_collect_candidates_for_model()`，避免与现有 `allocate_key()` 大段重复。
- 缺失 provider 插件时允许走中性容量分数，但仍以本地 `supported_models` 与核心状态做过滤。
- 仅第二条链路要求 `model` 必填；不要把第一条链路改成多职责入口。

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_api.py -k "allocate_by_model" -v`
Expected: 部分测试通过；若失败点转移到分配存储接口缺失，说明服务层筛选排序已接好。

**Step 5: Commit**

```bash
git add tests/test_api.py tests/test_domain.py tests/fakes.py src/application/services/key_service.py
git commit -m "feat: add service logic for cross-provider model allocation"
```

### Task 4: 扩展分配存储协议，支持跨 provider 租约抢占

**Files:**
- Modify: `src/domain/repositories/key_repository.py`
- Modify: `src/infrastructure/cache/key_cache.py`
- Modify: `tests/fakes.py`
- Test: `tests/test_api.py`

**Step 1: Write the failing test**

先让假实现暴露需求：新增测试验证服务层在第二条链路下会把“按分数排序后的跨 provider 候选”传给 allocation store，并最终取回正确 key。

```python
def test_allocate_by_model_uses_cross_provider_allocation_store_order() -> None:
    store = InMemoryAllocationStore()
    ...

    selected = anyio.run(service.allocate_key_by_model, "gpt-4o")

    assert store.last_any_provider_order == [
        ("openrouter", "key-openrouter"),
        ("openai", "key-openai"),
    ]
    assert selected.id == "key-openrouter"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_api.py -k "cross_provider_allocation_store_order" -v`
Expected: FAIL because `InMemoryAllocationStore` and `KeyAllocationStore` have no `allocate_key_any_provider()` method.

**Step 3: Write minimal implementation**

在协议、Redis 实现与测试假实现中新增跨 provider 分配接口：

```python
class KeyAllocationStore(Protocol):
    async def allocate_key_any_provider(
        self,
        ordered_candidates: list[tuple[str, str]],
        now: datetime,
        lease_seconds: int = 2,
    ) -> tuple[str, str] | None:
        ...
```

`InMemoryAllocationStore` 最小实现：

```python
async def allocate_key_any_provider(
    self,
    ordered_candidates: list[tuple[str, str]],
    now: datetime,
    lease_seconds: int = 2,
) -> tuple[str, str] | None:
    self.last_any_provider_order = list(ordered_candidates)
    if not ordered_candidates:
        return None
    return ordered_candidates[0]
```

`RedisKeyCache` 建议最小可行实现：
- 方案 A：按排序顺序逐个调用现有 `allocate_key(provider, [key_id], now, lease_seconds)`，直到某个 provider/key 成功拿到租约。
- 方案 B：新增跨 provider Lua 脚本，一次性遍历多个 provider 的 zset / lease zset。

本计划推荐先做 **方案 A**，原因：
- 改动更小，优先复用现有租约语义；
- 可以快速验证产品链路；
- 后续若性能成为问题，再单独优化为多 provider Lua。

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_api.py -k "allocate_by_model or cross_provider_allocation_store_order" -v`
Expected: PASS for allocation-store interaction tests.

**Step 5: Commit**

```bash
git add tests/test_api.py tests/fakes.py src/domain/repositories/key_repository.py src/infrastructure/cache/key_cache.py
git commit -m "feat: support cross-provider lease allocation"
```

### Task 5: 收敛 API 测试夹具，支持多 provider 测试场景

**Files:**
- Modify: `tests/test_api.py`
- Modify: `tests/fakes.py`

**Step 1: Write the failing test**

在现有 API 测试中补一个建模明确的测试夹具工厂，避免多个测试重复手写 `repository`、`provider_registry`、`allocation_store`。

```python
def test_build_cross_provider_client_supports_multiple_plugins() -> None:
    client = build_cross_provider_client()

    response = client.get("/api/providers")

    assert response.status_code == 200
    names = [item["name"] for item in response.json()]
    assert "openai" in names
    assert "openrouter" in names
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_api.py::test_build_cross_provider_client_supports_multiple_plugins -v`
Expected: FAIL because helper function does not exist.

**Step 3: Write minimal implementation**

在 `tests/test_api.py` 增加新的 client 构造器，或在 `tests/fakes.py` 增加工厂函数：

```python
def build_cross_provider_client(all_available: bool = True) -> TestClient:
    repository = InMemoryKeyRepository([...multi-provider keys...])
    allocation_store = InMemoryAllocationStore()
    scorer = KeyScorer()
    scheduler = KeyScheduler(scorer, jitter=0.0)
    state_machine = KeyStateMachine()
    provider_registry = build_provider_registry(
        FakeProviderPlugin("openai", ["gpt-4o"], available=all_available),
        FakeProviderPlugin("openrouter", ["gpt-4o"], available=all_available),
    )
    service = KeyService(repository, allocation_store, scheduler, scorer, state_machine, provider_registry)
    ...
    return TestClient(app)
```

这个步骤的目标是让后续第二条链路测试读起来更短、更稳。

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_api.py::test_build_cross_provider_client_supports_multiple_plugins -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_api.py tests/fakes.py
git commit -m "test: add reusable multi-provider API fixtures"
```

### Task 6: 补齐第一条与第二条链路并存时的回归测试

**Files:**
- Modify: `tests/test_api.py`
- Modify: `tests/test_domain.py`

**Step 1: Write the failing test**

补三类回归测试，确保新增第二条链路不会破坏第一条链路：

```python
def test_allocate_key_route_still_requires_provider() -> None:
    client = build_client()

    response = client.post(
        "/api/internal/allocate-key",
        json={"model": "gpt-4o"},
        headers={"X-Internal-Key": "test-key"},
    )

    assert response.status_code == 422
```

```python
def test_allocate_by_model_does_not_change_provider_scoped_selection() -> None:
    client = build_cross_provider_client()

    response = client.post(
        "/api/internal/allocate-key",
        json={"provider": "openai", "model": "gpt-4o"},
        headers={"X-Internal-Key": "test-key"},
    )

    assert response.status_code == 200
    assert response.json()["key_id"] == "key-openai"
```

```python
def test_allocate_by_model_returns_404_when_model_not_supported_anywhere() -> None:
    ...
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_api.py -k "allocate_key_route_still_requires_provider or allocate_by_model" -v`
Expected: FAIL until both routes and service behaviors are fully wired.

**Step 3: Write minimal implementation**

只做必要收尾：
- 修正新旧测试中的夹具和断言；
- 若需要，抽出共享 helper 以减少 `KeyService.allocate_key()` 与 `allocate_key_by_model()` 的重复；
- 保持第一条链路原样：`provider` 必填、返回中不增加 `provider` 字段。

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_api.py tests/test_domain.py -v`
Expected: PASS for all allocation-related tests.

**Step 5: Commit**

```bash
git add tests/test_api.py tests/test_domain.py src/application/services/key_service.py src/interfaces/api/routes/allocate.py
git commit -m "test: cover coexistence of provider and model allocation flows"
```

### Task 7: 更新接口与需求文档

**Files:**
- Modify: `docs/router.md`
- Modify: `docs/任务书.md`
- Optional Modify: `docs/plan.md`

**Step 1: Write the failing test**

这里用“文档校对清单”代替自动化测试，先列出必须更新的事实点：

```text
1. 新增 `/api/internal/allocate-by-model`
2. 请求体只包含 `model`
3. 响应新增 `provider`
4. 第一条链路保持不变
5. 明确第二条链路是跨 provider 候选统一评分
```

**Step 2: Run review to verify docs are outdated**

Run: `rg "allocate-by-model|跨 provider|provider.*credential" docs/router.md docs/任务书.md`
Expected: no matches or incomplete descriptions before editing.

**Step 3: Write minimal implementation**

更新文档时写清楚：
- 新接口路径、请求头、请求体、成功返回、错误返回；
- 第二条链路的业务说明：只传模型名，系统在所有 provider 的可用 key 中统一筛选；
- 与第一条链路的区别：第一条链路是“指定 provider 后在 provider 内选优”，第二条链路是“按 model 跨 provider 选优”；
- 如果文档里提到“返回凭证”，补充“同时返回 provider 名称”。

可直接给出示例：

```json
{
  "status": "ok",
  "key_id": "key-openrouter",
  "provider": "openrouter",
  "credential": {
    "api_key": "sk-or-best"
  }
}
```

**Step 4: Run review to verify docs are updated**

Run: `rg "allocate-by-model|跨 provider|\"provider\": \"openrouter\"" docs/router.md docs/任务书.md`
Expected: matches in both docs.

**Step 5: Commit**

```bash
git add docs/router.md docs/任务书.md docs/plan.md
git commit -m "docs: describe cross-provider model allocation flow"
```

### Task 8: 端到端验证与收尾

**Files:**
- Modify: `tests/test_api.py`
- Modify: `tests/fakes.py`
- Review: `src/interfaces/api/routes/allocate.py`
- Review: `src/application/services/key_service.py`
- Review: `src/infrastructure/cache/key_cache.py`

**Step 1: Write the verification checklist**

```text
1. 旧接口 `/api/internal/allocate-key` 继续工作
2. 新接口 `/api/internal/allocate-by-model` 可返回 provider + credential
3. 不支持模型时返回 404 no_available_key
4. 内部鉴权继续生效
5. report-success / report-error 不受影响
6. 多 provider 场景下租约释放仍按真实 provider 执行
```

**Step 2: Run targeted tests**

Run: `pytest tests/test_api.py -v`
Expected: PASS

**Step 3: Run broader regression tests**

Run: `pytest tests/test_domain.py tests/test_provider_plugins.py -v`
Expected: PASS

**Step 4: Run final focused search**

Run: `rg "allocate-by-model|allocate_key_by_model|allocate_key_any_provider" src tests docs`
Expected: route, service, store, tests, docs all have aligned references.

**Step 5: Commit**

```bash
git add src tests docs
git commit -m "feat: add cross-provider model allocation flow"
```

## Notes for the Implementer

- 不要把现有 `AllocateRequest` 改成 `provider` 可选；这是另一种 API 方案，本计划明确不采用。
- 第二条链路必须要求 `model` 非空；否则“跨 provider”没有筛选依据，容易退化成不受控的全局抢占。
- 第一版跨 provider 租约推荐复用现有按 provider 分配能力逐个尝试，先保证正确性；性能优化可后续单列任务。
- 提交粒度按任务切分，保证每一步都能独立回滚、独立 review。
