# Key Pool Tier Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add first-class `default` and `vip` key pools so one KeyFlow service can route default users and VIP users without running duplicate service instances.

**Architecture:** Store pool membership as `ApiKey` metadata, not inside provider credentials. Allocation requests pass a `KeyPool` enum value; `default` only searches the default pool, while `vip` searches the VIP pool first and falls back to default. Keep provider plugins unchanged and extend the existing repository, allocation store, and service layers.

**Tech Stack:** Python 3.13, FastAPI, Pydantic, SQLAlchemy asyncio, PostgreSQL, Redis Lua allocation, SQLite local runtime leases, pytest.

---

## Confirmed Product Rules

- `pool` is not part of `credential`.
- Current pools are enum values: `default` and `vip`.
- `provider + credential_fingerprint` remains globally unique across all pools.
- `default` requests never use `vip`.
- `vip` requests use `vip` first, then fallback to `default`.
- Allocation responses do not include `pool`.
- Existing `PUT /api/keys/{key_id}` does not change pool.
- Pool migration uses a dedicated admin endpoint.
- Pool migration preserves status, quota counters, success/error counts, model cache, refresh cache, and runtime fields.

## Pre-Implementation Checks

Run these before task execution:

```bash
git status --short
pytest tests/test_sqlite_local_runtime.py -q
pytest tests/test_provider_plugins.py -q
```

Expected:

- Git may already show unrelated local modifications; do not revert user changes.
- Existing tests should pass or any pre-existing failures must be recorded before editing.

---

### Task 1: Add `KeyPool` Domain Enum

**Files:**

- Create: `src/domain/value_objects/key_pool.py`
- Modify: `src/domain/entities/api_key.py`
- Test: `tests/test_key_pool.py`

**Step 1: Write the failing enum/entity test**

Create `tests/test_key_pool.py`:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-18
@Description: Key 池枚举与实体默认值测试
"""
from __future__ import annotations

from domain.entities.api_key import ApiKey
from domain.value_objects.key_pool import KeyPool


def test_api_key_defaults_to_default_pool() -> None:
    key = ApiKey(id="key-1", provider="openai", credential={"api_key": "sk-test"})

    assert key.pool == KeyPool.DEFAULT


def test_key_pool_values_are_stable_wire_values() -> None:
    assert KeyPool.DEFAULT.value == "default"
    assert KeyPool.VIP.value == "vip"
```

**Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_key_pool.py -q
```

Expected: FAIL because `domain.value_objects.key_pool` does not exist.

**Step 3: Add the enum**

Create `src/domain/value_objects/key_pool.py`:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-18
@Description: Key 池级别枚举
"""
from __future__ import annotations

from enum import Enum


class KeyPool(str, Enum):
    DEFAULT = "default"
    VIP = "vip"
```

**Step 4: Add `pool` to `ApiKey`**

Modify `src/domain/entities/api_key.py`:

```python
from domain.value_objects.key_pool import KeyPool
from domain.value_objects.key_status import KeyStatus


@dataclass(slots=True)
class ApiKey:
    id: str
    provider: str
    credential: dict[str, str]
    pool: KeyPool = KeyPool.DEFAULT
    status: KeyStatus = KeyStatus.AVAILABLE
```

Keep the existing credential comment and runtime fields.

**Step 5: Run test to verify it passes**

Run:

```bash
pytest tests/test_key_pool.py -q
```

Expected: PASS.

**Step 6: Run focused entity/import tests**

Run:

```bash
pytest tests/test_sqlite_local_runtime.py::test_sqlite_repository_maps_duplicate_credential_to_domain_error -q
```

Expected: PASS.

**Step 7: Commit**

Only commit if the user has explicitly requested commits for implementation:

```bash
git add src/domain/value_objects/key_pool.py src/domain/entities/api_key.py tests/test_key_pool.py
git commit -m "feat: add key pool enum"
```

---

### Task 2: Persist Pool in SQLAlchemy Repository

**Files:**

- Modify: `src/infrastructure/db/models.py`
- Modify: `src/infrastructure/db/repository_impl.py`
- Test: `tests/test_sqlite_local_runtime.py`
- Test: `tests/test_key_pool.py`

**Step 1: Add failing repository round-trip test**

Append to `tests/test_key_pool.py`:

```python
import pytest

from infrastructure.db.repository_impl import SqlAlchemyKeyRepository
from infrastructure.db.sqlite_bootstrap import bootstrap_sqlite_database
from infrastructure.db.sqlite_session import create_sqlite_session_factory


@pytest.mark.asyncio
async def test_repository_persists_key_pool(tmp_path) -> None:
    db_path = tmp_path / "keyflow.db"
    read_factory, write_factory = create_sqlite_session_factory(str(db_path))
    await bootstrap_sqlite_database(str(db_path), write_factory.kw["bind"])
    repository = SqlAlchemyKeyRepository(read_factory, write_factory)

    key = ApiKey(
        id="key-vip",
        provider="openai",
        credential={"api_key": "sk-vip"},
        pool=KeyPool.VIP,
    )

    await repository.upsert_key(key)
    persisted = await repository.get_key("key-vip")

    assert persisted is not None
    assert persisted.pool == KeyPool.VIP

    await write_factory.kw["bind"].dispose()
```

**Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_key_pool.py::test_repository_persists_key_pool -q
```

Expected: FAIL because `ApiKeyModel` has no `pool` column and repository mapping ignores it.

**Step 3: Add `pool` column to model**

Modify `src/infrastructure/db/models.py`:

```python
class ApiKeyModel(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    pool: Mapped[str] = mapped_column(String(32), default="default", index=True)
    credential: Mapped[dict] = mapped_column(JSON)
```

Do not modify `uq_api_keys_provider_credential`; it must remain `(provider, credential_fingerprint)`.

**Step 4: Map pool in repository writes**

Modify `SqlAlchemyKeyRepository.upsert_key()` in `src/infrastructure/db/repository_impl.py`:

```python
model.provider = key.provider
model.pool = key.pool.value
model.credential = key.credential
```

**Step 5: Map pool in repository reads**

Modify `_to_entity()` in `src/infrastructure/db/repository_impl.py` to pass:

```python
pool=KeyPool(model.pool or KeyPool.DEFAULT.value),
```

Add import:

```python
from domain.value_objects.key_pool import KeyPool
```

**Step 6: Run repository pool test**

Run:

```bash
pytest tests/test_key_pool.py::test_repository_persists_key_pool -q
```

Expected: PASS.

**Step 7: Run existing SQLite repository tests**

Run:

```bash
pytest tests/test_sqlite_local_runtime.py::test_sqlite_repository_maps_duplicate_credential_to_domain_error -q
pytest tests/test_sqlite_local_runtime.py::test_credential_fingerprint_is_stable_for_key_order -q
```

Expected: PASS.

**Step 8: Commit**

Only commit if authorized:

```bash
git add src/infrastructure/db/models.py src/infrastructure/db/repository_impl.py tests/test_key_pool.py
git commit -m "feat: persist key pool metadata"
```

---

### Task 3: Bootstrap PostgreSQL Pool Column

**Files:**

- Modify: `src/infrastructure/db/bootstrap.py`
- Test: `tests/test_sqlite_local_runtime.py`

**Step 1: Extend PostgreSQL bootstrap**

Modify `ensure_refresh_columns()` in `src/infrastructure/db/bootstrap.py`:

```python
for col, sql_type in [
    ("pool", "VARCHAR(32) DEFAULT 'default'"),
    ...
]:
    await conn.execute(
        text(f"ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS {col} {sql_type}")
    )
```

Add a follow-up backfill helper:

```python
async def backfill_key_pools(conn) -> None:
    await conn.execute(text("UPDATE api_keys SET pool = 'default' WHERE pool IS NULL"))
```

Call it in `bootstrap_write_database()` before uniqueness setup.

**Step 2: Verify SQLite bootstrap path**

`src/infrastructure/db/sqlite_bootstrap.py` currently uses `Base.metadata.create_all`. There is no existing SQLite database migration requirement for this feature, so new local SQLite files getting the `pool` column from `Base.metadata` is enough.

**Step 3: Run bootstrap-adjacent tests**

Run:

```bash
pytest tests/test_sqlite_local_runtime.py::test_sqlite_bootstrap_enables_wal_and_creates_schema -q
pytest tests/test_key_pool.py::test_repository_persists_key_pool -q
```

Expected: PASS.

**Step 4: Commit**

Only commit if authorized:

```bash
git add src/infrastructure/db/bootstrap.py
git commit -m "feat: bootstrap key pool column"
```

---

### Task 4: Add Repository Pool Query and Pool Migration Contract

**Files:**

- Modify: `src/domain/repositories/key_repository.py`
- Modify: `src/infrastructure/db/repository_impl.py`
- Test: `tests/test_key_pool.py`

**Step 1: Add failing tests for pool filtering and migration**

Append to `tests/test_key_pool.py`:

```python
@pytest.mark.asyncio
async def test_repository_lists_keys_by_provider_and_pool(tmp_path) -> None:
    db_path = tmp_path / "keyflow.db"
    read_factory, write_factory = create_sqlite_session_factory(str(db_path))
    await bootstrap_sqlite_database(str(db_path), write_factory.kw["bind"])
    repository = SqlAlchemyKeyRepository(read_factory, write_factory)

    await repository.upsert_key(
        ApiKey(id="default-key", provider="openai", credential={"api_key": "sk-default"})
    )
    await repository.upsert_key(
        ApiKey(
            id="vip-key",
            provider="openai",
            credential={"api_key": "sk-vip"},
            pool=KeyPool.VIP,
        )
    )

    default_keys = await repository.list_provider_pool_keys("openai", KeyPool.DEFAULT)
    vip_keys = await repository.list_provider_pool_keys("openai", KeyPool.VIP)

    assert [key.id for key in default_keys] == ["default-key"]
    assert [key.id for key in vip_keys] == ["vip-key"]

    await write_factory.kw["bind"].dispose()


@pytest.mark.asyncio
async def test_repository_updates_key_pool_without_resetting_runtime_fields(tmp_path) -> None:
    db_path = tmp_path / "keyflow.db"
    read_factory, write_factory = create_sqlite_session_factory(str(db_path))
    await bootstrap_sqlite_database(str(db_path), write_factory.kw["bind"])
    repository = SqlAlchemyKeyRepository(read_factory, write_factory)

    original = ApiKey(
        id="key-1",
        provider="openai",
        credential={"api_key": "sk-test"},
        success_count=3,
        error_count=1,
        quota_used=99,
    )
    await repository.upsert_key(original)

    migrated = await repository.update_pool("key-1", KeyPool.VIP)

    assert migrated is not None
    assert migrated.pool == KeyPool.VIP
    assert migrated.success_count == 3
    assert migrated.error_count == 1
    assert migrated.quota_used == 99

    await write_factory.kw["bind"].dispose()
```

**Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/test_key_pool.py::test_repository_lists_keys_by_provider_and_pool tests/test_key_pool.py::test_repository_updates_key_pool_without_resetting_runtime_fields -q
```

Expected: FAIL because repository protocol and implementation do not have these methods.

**Step 3: Extend repository protocol**

Modify `src/domain/repositories/key_repository.py`:

```python
from domain.value_objects.key_pool import KeyPool


class KeyRepository(Protocol):
    async def list_provider_pool_keys(self, provider: str, pool: KeyPool) -> list[ApiKey]:
        ...

    async def list_pool_keys(self, pool: KeyPool) -> list[ApiKey]:
        ...

    async def update_pool(self, key_id: str, pool: KeyPool) -> ApiKey | None:
        ...
```

**Step 4: Implement pool queries**

Modify `src/infrastructure/db/repository_impl.py`:

```python
async def list_provider_pool_keys(self, provider: str, pool: KeyPool) -> list[ApiKey]:
    async with self._read_factory() as session:
        stmt = (
            select(ApiKeyModel)
            .where(ApiKeyModel.provider == provider)
            .where(ApiKeyModel.pool == pool.value)
            .order_by(ApiKeyModel.provider, ApiKeyModel.id)
        )
        result = await session.execute(stmt)
        return [self._to_entity(row) for row in result.scalars().all()]


async def list_pool_keys(self, pool: KeyPool) -> list[ApiKey]:
    async with self._read_factory() as session:
        stmt = (
            select(ApiKeyModel)
            .where(ApiKeyModel.pool == pool.value)
            .order_by(ApiKeyModel.provider, ApiKeyModel.id)
        )
        result = await session.execute(stmt)
        return [self._to_entity(row) for row in result.scalars().all()]
```

**Step 5: Implement pool migration**

Add to `SqlAlchemyKeyRepository`:

```python
async def update_pool(self, key_id: str, pool: KeyPool) -> ApiKey | None:
    now = utcnow()
    async with self._write_factory() as session:
        stmt = (
            update(ApiKeyModel)
            .where(ApiKeyModel.id == key_id)
            .values(pool=pool.value, updated_at=now)
        )
        result = await session.execute(stmt)
        if result.rowcount == 0:
            await session.commit()
            return None
        await session.commit()
    return await self.get_key(key_id)
```

**Step 6: Run tests**

Run:

```bash
pytest tests/test_key_pool.py::test_repository_lists_keys_by_provider_and_pool tests/test_key_pool.py::test_repository_updates_key_pool_without_resetting_runtime_fields -q
```

Expected: PASS.

**Step 7: Commit**

Only commit if authorized:

```bash
git add src/domain/repositories/key_repository.py src/infrastructure/db/repository_impl.py tests/test_key_pool.py
git commit -m "feat: query and migrate key pools"
```

---

### Task 5: Make Allocation Store Pool-Aware

**Files:**

- Modify: `src/domain/repositories/key_repository.py`
- Modify: `src/infrastructure/cache/key_cache.py`
- Modify: `src/infrastructure/cache/sqlite_key_cache.py`
- Modify: `src/infrastructure/db/models.py`
- Test: `tests/test_key_pool.py`
- Test: `tests/test_sqlite_local_runtime.py`

**Step 1: Add failing SQLite lease isolation test**

Append to `tests/test_key_pool.py`:

```python
from infrastructure.cache.sqlite_key_cache import SqliteKeyCache
from datetime import datetime, timezone


@pytest.mark.asyncio
async def test_sqlite_leases_are_isolated_by_pool(tmp_path) -> None:
    db_path = tmp_path / "keyflow.db"
    read_factory, write_factory = create_sqlite_session_factory(str(db_path))
    await bootstrap_sqlite_database(str(db_path), write_factory.kw["bind"])
    repository = SqlAlchemyKeyRepository(read_factory, write_factory)
    cache = SqliteKeyCache(write_factory.kw["bind"])
    now = datetime.now(timezone.utc)

    await repository.upsert_key(
        ApiKey(id="key-default", provider="openai", credential={"api_key": "sk-default"})
    )
    await repository.upsert_key(
        ApiKey(
            id="key-vip",
            provider="openai",
            credential={"api_key": "sk-vip"},
            pool=KeyPool.VIP,
        )
    )

    default_id = await cache.allocate_key(
        "openai", KeyPool.DEFAULT, ["key-default"], now, lease_seconds=10
    )
    vip_id = await cache.allocate_key("openai", KeyPool.VIP, ["key-vip"], now, lease_seconds=10)

    assert default_id == "key-default"
    assert vip_id == "key-vip"

    await write_factory.kw["bind"].dispose()
```

**Step 2: Run test to verify failure**

Run:

```bash
pytest tests/test_key_pool.py::test_sqlite_leases_are_isolated_by_pool -q
```

Expected: FAIL because `allocate_key()` does not accept pool.

**Step 3: Update allocation store protocol**

Modify `src/domain/repositories/key_repository.py`:

```python
class KeyAllocationStore(Protocol):
    async def sync_key(self, key: ApiKey, score: float) -> None:
        ...

    async def remove_key(self, key_id: str, provider: str, pool: KeyPool) -> None:
        ...

    async def allocate_key(
        self,
        provider: str,
        pool: KeyPool,
        ordered_key_ids: list[str],
        now: datetime,
        lease_seconds: int = 2,
    ) -> str | None:
        ...

    async def allocate_key_any_provider(
        self,
        pool: KeyPool,
        ordered_keys: list[ApiKey],
        now: datetime,
        lease_seconds: int = 2,
    ) -> str | None:
        ...

    async def release_key_lease(self, provider: str, pool: KeyPool, key_id: str) -> None:
        ...
```

**Step 4: Update Redis key names**

Modify `src/infrastructure/cache/key_cache.py`:

```python
def _provider_zset(self, provider: str, pool: KeyPool) -> str:
    return f"keyflow:provider:{provider}:pool:{pool.value}:keys"


def _provider_lease_zset(self, provider: str, pool: KeyPool) -> str:
    return f"keyflow:provider:{provider}:pool:{pool.value}:leases"
```

`sync_key()` uses `key.pool`; `allocate_key()` and `release_key_lease()` use the passed pool.

**Step 5: Update SQLite lease model**

Modify `src/infrastructure/db/models.py`:

```python
class KeyLeaseModel(Base):
    __tablename__ = "key_leases"

    key_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    pool: Mapped[str] = mapped_column(String(32), default="default", index=True)
```

**Step 6: Update SQLite allocation store**

Modify `src/infrastructure/cache/sqlite_key_cache.py`:

- `remove_key(self, key_id, provider, pool)` calls `release_key_lease(provider, pool, key_id)`.
- `allocate_key(self, provider, pool, ordered_key_ids, now, lease_seconds)` deletes expired leases for `provider + pool`.
- Candidate row select includes `ApiKeyModel.pool`.
- Candidate usability check requires `key["pool"] == pool.value`.
- Lease insert/update stores `pool=pool.value`.
- `allocate_key_any_provider(self, pool, ordered_keys, now, lease_seconds)` passes pool to `allocate_key`.
- `release_key_lease(self, provider, pool, key_id)` deletes by provider + pool + key_id.

**Step 7: Update existing SQLite tests to pass pool**

Modify `tests/test_sqlite_local_runtime.py` allocation calls:

```python
from domain.value_objects.key_pool import KeyPool

await cache.allocate_key("openai", KeyPool.DEFAULT, ["key-1"], now, lease_seconds=10)
await cache.release_key_lease("openai", KeyPool.DEFAULT, "key-1")
```

**Step 8: Run focused tests**

Run:

```bash
pytest tests/test_key_pool.py::test_sqlite_leases_are_isolated_by_pool -q
pytest tests/test_sqlite_local_runtime.py -q
```

Expected: PASS.

**Step 9: Commit**

Only commit if authorized:

```bash
git add src/domain/repositories/key_repository.py src/infrastructure/cache/key_cache.py src/infrastructure/cache/sqlite_key_cache.py src/infrastructure/db/models.py tests/test_key_pool.py tests/test_sqlite_local_runtime.py
git commit -m "feat: isolate allocation leases by key pool"
```

---

### Task 6: Add Pool-Aware Allocation Service Logic

**Files:**

- Modify: `src/application/services/key_service.py`
- Test: `tests/test_key_pool.py`
- Inspect: `tests/fakes.py`

**Step 1: Add failing service allocation tests**

Append to `tests/test_key_pool.py`:

```python
from application.services.key_service import KeyService
from domain.services.scheduler import KeyScheduler
from domain.services.scorer import KeyScorer
from domain.services.state_machine import KeyStateMachine
from tests.fakes import FakeProviderPlugin, build_provider_registry


def _build_service(repository, cache) -> KeyService:
    scorer = KeyScorer()
    return KeyService(
        repository,
        cache,
        KeyScheduler(scorer),
        scorer,
        KeyStateMachine(),
        build_provider_registry(FakeProviderPlugin("openai", models=["gpt-4o"])),
        allocation_lease_seconds=10,
    )


@pytest.mark.asyncio
async def test_default_allocation_does_not_use_vip_pool(tmp_path) -> None:
    db_path = tmp_path / "keyflow.db"
    read_factory, write_factory = create_sqlite_session_factory(str(db_path))
    await bootstrap_sqlite_database(str(db_path), write_factory.kw["bind"])
    repository = SqlAlchemyKeyRepository(read_factory, write_factory)
    cache = SqliteKeyCache(write_factory.kw["bind"])
    service = _build_service(repository, cache)

    await repository.upsert_key(
        ApiKey(
            id="vip-key",
            provider="openai",
            credential={"api_key": "sk-vip"},
            pool=KeyPool.VIP,
            supported_models=["gpt-4o"],
        )
    )

    with pytest.raises(NoAvailableKeyError):
        await service.allocate_key("openai", "gpt-4o", pool=KeyPool.DEFAULT)

    await write_factory.kw["bind"].dispose()


@pytest.mark.asyncio
async def test_vip_allocation_falls_back_to_default_pool(tmp_path) -> None:
    db_path = tmp_path / "keyflow.db"
    read_factory, write_factory = create_sqlite_session_factory(str(db_path))
    await bootstrap_sqlite_database(str(db_path), write_factory.kw["bind"])
    repository = SqlAlchemyKeyRepository(read_factory, write_factory)
    cache = SqliteKeyCache(write_factory.kw["bind"])
    service = _build_service(repository, cache)

    await repository.upsert_key(
        ApiKey(
            id="default-key",
            provider="openai",
            credential={"api_key": "sk-default"},
            supported_models=["gpt-4o"],
        )
    )

    allocation = await service.allocate_key("openai", "gpt-4o", pool=KeyPool.VIP)

    assert allocation.key.id == "default-key"

    await write_factory.kw["bind"].dispose()
```

Add missing imports:

```python
from datetime import datetime, timezone
from domain.exceptions.domain_exceptions import NoAvailableKeyError
```

**Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/test_key_pool.py::test_default_allocation_does_not_use_vip_pool tests/test_key_pool.py::test_vip_allocation_falls_back_to_default_pool -q
```

Expected: FAIL because service methods do not accept pool.

**Step 3: Add pool sequence helper**

Modify `src/application/services/key_service.py`:

```python
from domain.value_objects.key_pool import KeyPool


def _allocation_pool_sequence(pool: KeyPool) -> tuple[KeyPool, ...]:
    if pool == KeyPool.VIP:
        return (KeyPool.VIP, KeyPool.DEFAULT)
    return (KeyPool.DEFAULT,)
```

**Step 4: Update `AllocationResult` only if needed**

Do not add pool to public response. `AllocationResult` can stay unchanged unless internal debugging requires it.

**Step 5: Update `allocate_key()` signature and loop**

Change:

```python
async def allocate_key(
    self,
    provider: str,
    model: str | None = None,
    pool: KeyPool = KeyPool.DEFAULT,
) -> AllocationResult:
```

Implementation pattern:

```python
provider = provider.strip().lower()
last_error: NoAvailableKeyError | None = None
for candidate_pool in _allocation_pool_sequence(pool):
    keys = await self._repository.list_provider_pool_keys(provider, candidate_pool)
    await self._recover_ready_keys(keys, now)
    candidates, capacity_by_key_id, provider_model_by_key_id = await self._collect_candidates(
        keys, model, now
    )
    ranked = self._scheduler.rank_keys(candidates, now, capacity_by_key_id=capacity_by_key_id)
    if not ranked:
        last_error = NoAvailableKeyError("no available key")
        continue
    allocated_id = await self._allocation_store.allocate_key(
        provider,
        candidate_pool,
        [item.key.id for item in ranked],
        now,
        lease_seconds=self._allocation_lease_seconds,
    )
    if allocated_id is None:
        last_error = NoAvailableKeyError("no available key")
        continue
    return await self._finalize_allocation(ranked, allocated_id, now, provider_model_by_key_id)
raise last_error or NoAvailableKeyError("no available key")
```

**Step 6: Update `allocate_key_by_model()` similarly**

Signature:

```python
async def allocate_key_by_model(
    self,
    model: str,
    pool: KeyPool = KeyPool.DEFAULT,
) -> AllocationResult:
```

Use `list_pool_keys(candidate_pool)` and `allocate_key_any_provider(candidate_pool, ...)`.

**Step 7: Update release/sync calls for new allocation store API**

In `KeyService`, update:

```python
await self._allocation_store.release_key_lease(key.provider, key.pool, key.id)
await self._allocation_store.remove_key(key.id, key.provider, key.pool)
```

Use `selected.pool` in `_finalize_allocation()` when releasing lease on failure.

**Step 8: Run service tests**

Run:

```bash
pytest tests/test_key_pool.py::test_default_allocation_does_not_use_vip_pool tests/test_key_pool.py::test_vip_allocation_falls_back_to_default_pool -q
```

Expected: PASS.

**Step 9: Run broader service-adjacent tests**

Run:

```bash
pytest tests/test_key_pool.py -q
pytest tests/test_sqlite_local_runtime.py -q
```

Expected: PASS.

**Step 10: Commit**

Only commit if authorized:

```bash
git add src/application/services/key_service.py tests/test_key_pool.py
git commit -m "feat: route allocation by key pool"
```

---

### Task 7: Expose Pool in API Requests and Admin Create

**Files:**

- Modify: `src/interfaces/schemas/request.py`
- Modify: `src/interfaces/api/routes/allocate.py`
- Modify: `src/interfaces/api/routes/admin.py`
- Test: `tests/test_key_pool.py`

**Step 1: Add failing schema tests**

Append to `tests/test_key_pool.py`:

```python
from interfaces.schemas.request import AllocateByModelRequest, AllocateRequest, CreateKeyRequest


def test_allocate_request_defaults_pool_to_default() -> None:
    request = AllocateRequest(provider="openai", model="gpt-4o")

    assert request.pool == KeyPool.DEFAULT


def test_allocate_request_accepts_vip_pool() -> None:
    request = AllocateRequest(provider="openai", model="gpt-4o", pool="vip")

    assert request.pool == KeyPool.VIP


def test_allocate_by_model_request_defaults_pool_to_default() -> None:
    request = AllocateByModelRequest(model="gpt-4o")

    assert request.pool == KeyPool.DEFAULT


def test_create_key_request_accepts_pool_metadata_outside_credential() -> None:
    request = CreateKeyRequest(credential={"api_key": "sk-test"}, pool="vip")

    assert request.pool == KeyPool.VIP
    assert "pool" not in request.credential
```

**Step 2: Run schema tests to verify failure**

Run:

```bash
pytest tests/test_key_pool.py::test_allocate_request_defaults_pool_to_default tests/test_key_pool.py::test_create_key_request_accepts_pool_metadata_outside_credential -q
```

Expected: FAIL because request schemas have no `pool`.

**Step 3: Update request schemas**

Modify `src/interfaces/schemas/request.py`:

```python
from domain.value_objects.key_pool import KeyPool


class AllocateRequest(BaseModel):
    provider: str
    model: str | None = None
    pool: KeyPool = KeyPool.DEFAULT


class AllocateByModelRequest(BaseModel):
    model: str
    pool: KeyPool = KeyPool.DEFAULT


class CreateKeyRequest(BaseModel):
    credential: dict[str, str]
    pool: KeyPool = KeyPool.DEFAULT
```

Do not add pool to `UpdateKeyRequest`.

**Step 4: Update allocation routes**

Modify `src/interfaces/api/routes/allocate.py`:

```python
allocation = await service.allocate_key(payload.provider, payload.model, pool=payload.pool)
```

and:

```python
allocation = await service.allocate_key_by_model(payload.model, pool=payload.pool)
```

Do not add pool to `AllocateResponse` or `AllocateByModelResponse`.

**Step 5: Update admin create route**

Modify `src/interfaces/api/routes/admin.py`:

```python
key = await service.create_key(
    CreateKeyInput(provider=provider, credential=payload.credential, pool=payload.pool)
)
```

**Step 6: Update `CreateKeyInput`**

Modify `src/application/services/key_service.py`:

```python
@dataclass(slots=True)
class CreateKeyInput:
    provider: str
    credential: dict[str, str]
    pool: KeyPool = KeyPool.DEFAULT
```

Use it in `create_key()`:

```python
key = ApiKey(
    id=str(uuid4()),
    provider=provider,
    credential=credential,
    pool=data.pool,
    status=KeyStatus.PENDING,
    last_refreshed_at=now,
)
```

**Step 7: Run schema/API unit tests**

Run:

```bash
pytest tests/test_key_pool.py::test_allocate_request_defaults_pool_to_default tests/test_key_pool.py::test_allocate_request_accepts_vip_pool tests/test_key_pool.py::test_allocate_by_model_request_defaults_pool_to_default tests/test_key_pool.py::test_create_key_request_accepts_pool_metadata_outside_credential -q
```

Expected: PASS.

**Step 8: Commit**

Only commit if authorized:

```bash
git add src/interfaces/schemas/request.py src/interfaces/api/routes/allocate.py src/interfaces/api/routes/admin.py src/application/services/key_service.py tests/test_key_pool.py
git commit -m "feat: accept key pool in allocation requests"
```

---

### Task 8: Add Dedicated Pool Migration API

**Files:**

- Modify: `src/interfaces/schemas/request.py`
- Modify: `src/interfaces/api/routes/admin.py`
- Modify: `src/application/services/key_service.py`
- Test: `tests/test_key_pool.py`

**Step 1: Add failing service migration test**

Append to `tests/test_key_pool.py`:

```python
@pytest.mark.asyncio
async def test_service_moves_key_pool_and_preserves_statistics(tmp_path) -> None:
    db_path = tmp_path / "keyflow.db"
    read_factory, write_factory = create_sqlite_session_factory(str(db_path))
    await bootstrap_sqlite_database(str(db_path), write_factory.kw["bind"])
    repository = SqlAlchemyKeyRepository(read_factory, write_factory)
    cache = SqliteKeyCache(write_factory.kw["bind"])
    service = _build_service(repository, cache)

    await repository.upsert_key(
        ApiKey(
            id="key-1",
            provider="openai",
            credential={"api_key": "sk-test"},
            success_count=5,
            error_count=2,
            quota_used=123,
        )
    )

    migrated = await service.move_key_pool("key-1", KeyPool.VIP)

    assert migrated.pool == KeyPool.VIP
    assert migrated.success_count == 5
    assert migrated.error_count == 2
    assert migrated.quota_used == 123

    await write_factory.kw["bind"].dispose()
```

**Step 2: Run test to verify failure**

Run:

```bash
pytest tests/test_key_pool.py::test_service_moves_key_pool_and_preserves_statistics -q
```

Expected: FAIL because `move_key_pool()` does not exist.

**Step 3: Add request schema**

Modify `src/interfaces/schemas/request.py`:

```python
class MoveKeyPoolRequest(BaseModel):
    pool: KeyPool
```

**Step 4: Add service method**

Modify `src/application/services/key_service.py`:

```python
async def move_key_pool(self, key_id: str, pool: KeyPool) -> ApiKey:
    key = await self._get_required_key(key_id)
    if key.pool == pool:
        return key

    migrated = await self._repository.update_pool(key.id, pool)
    if migrated is None:
        raise KeyNotFoundError(f"key {key_id} not found")
    await self._allocation_store.remove_key(key.id, key.provider, key.pool)
    await self._allocation_store.sync_key(migrated, self._scorer.score(migrated, utcnow()))
    return migrated
```

This updates the database first, then removes the key from the old pool's allocation index and lease set, then syncs it into the new pool. Runtime counters and cached fields stay unchanged because repository only updates `pool` and `updated_at`.

**Step 5: Add admin endpoint**

Modify imports in `src/interfaces/api/routes/admin.py`:

```python
from interfaces.schemas.request import CreateKeyRequest, MoveKeyPoolRequest, UpdateKeyRequest
```

Add route before generic `/keys/{key_id}` detail if needed to avoid path ambiguity:

```python
@router.put("/keys/{key_id}/pool", response_model=OperationStatusResponse)
async def move_key_pool(
    key_id: str,
    payload: MoveKeyPoolRequest,
    service: KeyService = Depends(get_key_service),
) -> OperationStatusResponse:
    try:
        await service.move_key_pool(key_id, payload.pool)
    except KeyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key_not_found") from exc
    return OperationStatusResponse(status="ok")
```

**Step 6: Run service migration test**

Run:

```bash
pytest tests/test_key_pool.py::test_service_moves_key_pool_and_preserves_statistics -q
```

Expected: PASS.

**Step 7: Add optional FastAPI route test**

If the current test suite already has app-level internal-key tests, add a `TestClient` test that:

- Creates or seeds a key.
- Calls `PUT /api/keys/{key_id}/pool` with `{"pool": "vip"}`.
- Asserts `{"status": "ok"}`.
- Reads repository state and confirms `pool == KeyPool.VIP`.

**Step 8: Commit**

Only commit if authorized:

```bash
git add src/interfaces/schemas/request.py src/interfaces/api/routes/admin.py src/application/services/key_service.py tests/test_key_pool.py
git commit -m "feat: add key pool migration endpoint"
```

---

### Task 9: Update Admin Views Without Leaking Allocation Pool

**Files:**

- Modify: `src/interfaces/schemas/response.py`
- Modify: `src/interfaces/api/routes/admin.py`
- Test: `tests/test_key_pool.py`

**Step 1: Add failing response contract test**

Append to `tests/test_key_pool.py`:

```python
from interfaces.schemas.response import AllocateResponse, AllocateByModelResponse


def test_allocation_responses_do_not_expose_pool() -> None:
    allocate = AllocateResponse(
        key_id="key-1",
        provider_model="gpt-4o",
        credential={"api_key": "sk-test"},
    )
    by_model = AllocateByModelResponse(
        key_id="key-1",
        provider="openai",
        provider_model="gpt-4o",
        credential={"api_key": "sk-test"},
    )

    assert "pool" not in allocate.model_dump()
    assert "pool" not in by_model.model_dump()
```

This should already pass if no one added pool to allocation responses.

**Step 2: Add admin-only pool exposure**

Admin key list/detail should include `pool`, because operators need to know which pool a key belongs to. Allocation responses and report responses must not include pool.

Add to response schemas:

```python
from domain.value_objects.key_pool import KeyPool


class AdminKeyListItemResponse(BaseModel):
    key_id: str
    credential: dict[str, str]
    pool: KeyPool
    status: KeyStatus


class AdminKeyDetailResponse(BaseModel):
    credential: dict[str, str]
    pool: KeyPool
    status: KeyStatus
```

Do not modify `KeyResponse`, because it is used by `/report-success` and `/report-error`. Update only `_to_list_item_response()` and `_to_detail_response()` in `src/interfaces/api/routes/admin.py` to include `pool=key.pool`.

**Step 3: Add admin response mapping test**

If there is no existing route-level test, test the response model directly:

```python
from interfaces.schemas.response import AdminKeyListItemResponse
from domain.value_objects.key_status import KeyStatus


def test_admin_key_response_can_show_pool() -> None:
    response = AdminKeyListItemResponse(
        key_id="key-1",
        credential={"api_key": "sk-test"},
        pool=KeyPool.VIP,
        status=KeyStatus.AVAILABLE,
    )

    assert response.pool == KeyPool.VIP
```

**Step 4: Run response tests**

Run:

```bash
pytest tests/test_key_pool.py::test_allocation_responses_do_not_expose_pool tests/test_key_pool.py::test_admin_key_response_can_show_pool -q
```

Expected: PASS.

**Step 5: Commit**

Only commit if authorized:

```bash
git add src/interfaces/schemas/response.py src/interfaces/api/routes/admin.py tests/test_key_pool.py
git commit -m "feat: show key pool in admin responses"
```

---

### Task 10: Update Docs

**Files:**

- Modify: `docs/router.md`
- Modify: `docs/调度算法.md`
- Optional Modify: `.env.example` only if examples mention pool-aware requests

**Step 1: Update router docs**

Modify `docs/router.md`:

- For `POST /api/internal/allocate-key`, add `pool` to request body examples.
- For `POST /api/internal/allocate-by-model`, add `pool`.
- Document `pool` values: `default`, `vip`.
- State `pool` defaults to `default`.
- State allocation response does not return pool.
- Add route row and section for `PUT /api/keys/{key_id}/pool`.

Example:

```json
{
  "provider": "openai",
  "model": "gpt-4o",
  "pool": "vip"
}
```

**Step 2: Update scheduling docs**

Modify `docs/调度算法.md`:

- Update core problem statement to include pool.
- Add pool selection before provider/model filtering:

```text
requested_pool=default -> [default]
requested_pool=vip -> [vip, default]
```

- Explain default never falls back to vip.
- Explain vip fallback is for availability, not response disclosure.

**Step 3: Run docs-free tests**

Run:

```bash
pytest tests/test_key_pool.py -q
```

Expected: PASS.

**Step 4: Commit**

Only commit if authorized:

```bash
git add docs/router.md docs/调度算法.md .env.example
git commit -m "docs: document key pool allocation"
```

---

### Task 11: Full Verification

**Files:**

- Verify all changed files.

**Step 1: Run focused pool tests**

Run:

```bash
pytest tests/test_key_pool.py -q
```

Expected: PASS.

**Step 2: Run runtime allocation tests**

Run:

```bash
pytest tests/test_sqlite_local_runtime.py -q
```

Expected: PASS.

**Step 3: Run provider contract tests**

Run:

```bash
pytest tests/test_provider_plugins.py -q
```

Expected: PASS.

**Step 4: Run broader test suite if time allows**

Run:

```bash
pytest -q
```

Expected: PASS, or document any unrelated pre-existing failures.

**Step 5: Run lint diagnostics in Cursor**

Use Cursor lints on changed paths:

- `src/domain/entities/api_key.py`
- `src/domain/value_objects/key_pool.py`
- `src/domain/repositories/key_repository.py`
- `src/infrastructure/db/models.py`
- `src/infrastructure/db/repository_impl.py`
- `src/infrastructure/db/bootstrap.py`
- `src/infrastructure/cache/key_cache.py`
- `src/infrastructure/cache/sqlite_key_cache.py`
- `src/application/services/key_service.py`
- `src/interfaces/schemas/request.py`
- `src/interfaces/schemas/response.py`
- `src/interfaces/api/routes/allocate.py`
- `src/interfaces/api/routes/admin.py`
- `tests/test_key_pool.py`
- `tests/test_sqlite_local_runtime.py`

Expected: no newly introduced diagnostics.

**Step 6: Final commit**

Only commit if authorized and prior task commits were not made:

```bash
git add src/domain src/infrastructure src/application src/interfaces tests docs
git commit -m "feat: add key pool tier allocation"
```

---

## Implementation Notes

- Do not add `pool` into provider credentials.
- Do not change provider plugin interfaces.
- Do not include pool in allocation responses.
- Keep `provider + credential_fingerprint` unique across all pools.
- Prefer using `KeyPool` in Python APIs instead of raw strings.
- Convert Pydantic input into `KeyPool` at the schema boundary.
- Use `key.pool` when syncing/removing/releasing allocation leases.
- Treat pool migration to the same value as idempotent success.
- If a Redis migration path is needed, old keys under `keyflow:provider:{provider}:keys` may become stale. Prefer the new pool-aware keys and let fresh `sync_key()` calls repopulate them; old keys can be cleaned manually if necessary.

## Execution Handoff

Plan complete and saved to `docs/plans/2026-05-18-key-pool-tier.md`. Two execution options:

**1. Subagent-Driven (this session)** - Dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Parallel Session (separate)** - Open a new session with executing-plans and batch execution with checkpoints.

Which approach?
