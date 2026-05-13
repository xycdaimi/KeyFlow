# Local SQLite Runtime Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `local` runtime mode that runs API workers and the background worker in one container without Redis or PostgreSQL, using SQLite WAL transactions for cross-process state and allocation leases.

**Architecture:** Keep the application layer unchanged by preserving the existing `KeyRepository` and `KeyAllocationStore` interfaces. `dev` mode keeps the current PostgreSQL + Redis implementation; `local` mode uses SQLAlchemy async SQLite for key persistence and a SQLite-backed allocation store whose lease acquisition is guarded by `BEGIN IMMEDIATE` transactions.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy asyncio, SQLite WAL, aiosqlite, Docker Compose, pytest.

---

## File Structure

- Modify: `pyproject.toml`
  - Add `aiosqlite` so SQLAlchemy can use `sqlite+aiosqlite`.
- Modify: `.env.example`
  - Document `KEYFLOW_RUNTIME_MODE`, `LOCAL_SQLITE_PATH`, and local/dev examples.
- Modify: `src/infrastructure/config/settings.py`
  - Add `runtime_mode` and `local_sqlite_path`.
- Modify: `src/infrastructure/db/models.py`
  - Add `credential_fingerprint` and `KeyLeaseModel`.
  - Replace the PostgreSQL-only credential uniqueness expression with a dialect-neutral `(provider, credential_fingerprint)` unique index.
- Modify: `src/infrastructure/db/repository_impl.py`
  - Populate `credential_fingerprint` on writes.
  - Keep the existing repository behavior.
- Modify: `src/infrastructure/db/bootstrap.py`
  - Migrate the new fingerprint column/index in the same task as the model/repository change.
- Create: `src/infrastructure/db/sqlite_session.py`
  - Build SQLite async session factories and configure WAL pragmas.
- Create: `src/infrastructure/db/sqlite_bootstrap.py`
  - Create local database directory and schema.
- Create: `src/infrastructure/cache/sqlite_key_cache.py`
  - Implement `KeyAllocationStore` using SQLite leases and transactions.
- Modify: `src/container/container.py`
  - Select dev or local infrastructure by `KEYFLOW_RUNTIME_MODE`.
- Modify: `src/interfaces/api/app.py`
  - Bootstrap local SQLite and attach mode-specific health checks.
- Create: `scripts/run_local_container.sh`
  - Start background worker and uvicorn in the same container.
- Create: `docker/local/docker-compose.yml`
  - Make command switch through the local entrypoint and mount `/data`.
- Create: `tests/test_sqlite_local_runtime.py`
  - Cover SQLite WAL bootstrap, lease concurrency, release, expiry, and container mode wiring.

## Runtime Rules

- `KEYFLOW_RUNTIME_MODE=dev`
  - Uses current PostgreSQL + Redis path.
  - Health checks include app, database, Redis.
- `KEYFLOW_RUNTIME_MODE=local`
  - Uses one SQLite file at `LOCAL_SQLITE_PATH`.
  - API and worker processes share that file.
  - Health checks include app and database, not Redis. The public health field remains `database` even when the backend is SQLite.
  - Supported deployment shape is one container, one host filesystem, multiple processes inside that container.
  - Horizontal multi-container deployment is explicitly unsupported.

---

### Task 1: Add Runtime Configuration

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/infrastructure/config/settings.py`
- Modify: `.env.example`
- Test: `tests/test_sqlite_local_runtime.py`

- [ ] **Step 1: Write failing settings tests**

Create `tests/test_sqlite_local_runtime.py` with:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-13
@Description: local SQLite 运行模式测试
"""
from __future__ import annotations

from infrastructure.config.settings import Settings


def test_settings_exposes_local_runtime_mode(tmp_path) -> None:
    db_path = tmp_path / "keyflow.db"

    settings = Settings(
        KEYFLOW_RUNTIME_MODE="local",
        LOCAL_SQLITE_PATH=str(db_path),
    )

    assert settings.runtime_mode == "local"
    assert settings.local_sqlite_path == str(db_path)


def test_settings_defaults_to_dev_runtime_mode() -> None:
    settings = Settings(_env_file=None)

    assert settings.runtime_mode == "dev"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_sqlite_local_runtime.py -q`

Expected: FAIL with an error like `AttributeError: 'Settings' object has no attribute 'runtime_mode'`.

- [ ] **Step 3: Add dependency**

Modify `pyproject.toml` dependencies:

```toml
dependencies = [
  "fastapi>=0.115.0",
  "pydantic>=2.8.0",
  "pydantic-settings>=2.4.0",
  "punq>=0.7.0",
  "redis>=5.0.0",
  "sqlalchemy[asyncio]>=2.0.0",
  "asyncpg>=0.29.0",
  "aiosqlite>=0.20.0",
  "uvicorn[standard]>=0.30.0",
  "httpx>=0.27.0",
  "PyYAML>=6.0.2"
]
```

- [ ] **Step 4: Add settings fields**

Modify `src/infrastructure/config/settings.py` inside `Settings`:

```python
    runtime_mode: str = Field(default="dev", alias="KEYFLOW_RUNTIME_MODE")
    """Runtime mode: dev uses PostgreSQL + Redis; local uses SQLite WAL only."""
    local_sqlite_path: str = Field(default="/data/keyflow.db", alias="LOCAL_SQLITE_PATH")
    """SQLite database path used when KEYFLOW_RUNTIME_MODE=local."""
```

- [ ] **Step 5: Document env values**

Add to `.env.example` near database settings:

```env
# Runtime mode:
# - dev: PostgreSQL + Redis
# - local: single-container SQLite WAL, no PostgreSQL/Redis
KEYFLOW_RUNTIME_MODE=dev
LOCAL_SQLITE_PATH=/data/keyflow.db
```

- [ ] **Step 6: Run tests and verify pass**

Run: `python -m pytest tests/test_sqlite_local_runtime.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .env.example src/infrastructure/config/settings.py tests/test_sqlite_local_runtime.py
git commit -m "feat: add runtime mode settings"
```

---

### Task 2: Make Credential Uniqueness Dialect-Neutral

**Files:**
- Modify: `src/infrastructure/db/models.py`
- Modify: `src/infrastructure/db/repository_impl.py`
- Modify: `src/infrastructure/db/bootstrap.py`
- Test: `tests/test_sqlite_local_runtime.py`

- [ ] **Step 1: Add repository fingerprint test**

Append to `tests/test_sqlite_local_runtime.py`:

```python
import hashlib
import json

from infrastructure.db.repository_impl import credential_fingerprint


def test_credential_fingerprint_is_stable_for_key_order() -> None:
    left = {"api_key": "sk-test", "org": "org-1"}
    right = {"org": "org-1", "api_key": "sk-test"}
    expected = hashlib.sha256(
        json.dumps(left, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert credential_fingerprint(left) == expected
    assert credential_fingerprint(right) == expected
```

- [ ] **Step 2: Run test and verify failure**

Run: `python -m pytest tests/test_sqlite_local_runtime.py::test_credential_fingerprint_is_stable_for_key_order -q`

Expected: FAIL with `ImportError` or `NameError` for `credential_fingerprint`.

- [ ] **Step 3: Update model**

Modify `src/infrastructure/db/models.py` imports:

```python
from sqlalchemy import JSON, DateTime, Float, Index, Integer, String
```

Replace `ApiKeyModel.__table_args__` with:

```python
    __table_args__ = (
        Index(
            "uq_api_keys_provider_credential",
            "provider",
            "credential_fingerprint",
            unique=True,
        ),
    )
```

Add this column after `credential`:

```python
    credential_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    """SHA-256 fingerprint of normalized credential JSON for dialect-neutral uniqueness."""
```

Add lease model at the end of the file:

```python
class KeyLeaseModel(Base):
    __tablename__ = "key_leases"

    key_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

- [ ] **Step 4: Update repository fingerprinting**

Modify `src/infrastructure/db/repository_impl.py` imports:

```python
import hashlib
import json
from datetime import datetime, timedelta, timezone
```

Add helper near `utcnow()`:

```python
def credential_fingerprint(credential: dict) -> str:
    normalized = json.dumps(credential, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
```

Inside `upsert_key`, after `model.credential = key.credential`, add:

```python
                model.credential_fingerprint = credential_fingerprint(key.credential)
```

Inside both `update_runtime_snapshot_if_locked` and `update_background_runtime_snapshot_if_locked` `.values(...)`, add:

```python
                    credential_fingerprint=credential_fingerprint(key.credential),
```

Replace `_is_provider_credential_unique_violation` with:

```python
    @staticmethod
    def _is_provider_credential_unique_violation(exc: IntegrityError) -> bool:
        message = str(exc.orig).lower()
        return (
            "uq_api_keys_provider_credential" in message
            or (
                "unique constraint failed" in message
                and "api_keys.provider" in message
                and "api_keys.credential_fingerprint" in message
            )
        )
```

Inside `_to_entity`, no entity field is needed; leave return shape unchanged.

- [ ] **Step 5: Update PostgreSQL bootstrap migration**

Modify `src/infrastructure/db/bootstrap.py`.

Add import:

```python
from infrastructure.db.repository_impl import credential_fingerprint
```

Modify `ensure_refresh_columns` to include the nullable column addition:

```python
        ("credential_fingerprint", "VARCHAR(64)"),
```

Add:

```python
async def backfill_credential_fingerprints(conn) -> None:
    result = await conn.execute(text("SELECT id, credential FROM api_keys"))
    rows = result.mappings().all()
    for row in rows:
        await conn.execute(
            text(
                "UPDATE api_keys SET credential_fingerprint = :fingerprint "
                "WHERE id = :id AND credential_fingerprint IS NULL"
            ),
            {
                "id": row["id"],
                "fingerprint": credential_fingerprint(row["credential"]),
            },
        )
```

Add:

```python
async def ensure_credential_fingerprint_not_null(conn) -> None:
    await conn.execute(
        text("ALTER TABLE api_keys ALTER COLUMN credential_fingerprint SET NOT NULL")
    )
```

Replace `ensure_credential_uniqueness` with:

```python
async def ensure_credential_uniqueness(conn) -> None:
    await conn.execute(text("DROP INDEX IF EXISTS uq_api_keys_provider_credential"))
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_api_keys_provider_credential "
            "ON api_keys (provider, credential_fingerprint)"
        )
    )
```

In `bootstrap_write_database`, call the migration pieces in this order:

```python
        await ensure_refresh_columns(conn)
        await backfill_credential_fingerprints(conn)
        await ensure_credential_fingerprint_not_null(conn)
        await ensure_credential_uniqueness(conn)
```

- [ ] **Step 6: Add SQLite uniqueness error recognition test**

Append to `tests/test_sqlite_local_runtime.py`:

```python
from sqlalchemy.exc import IntegrityError

from infrastructure.db.repository_impl import SqlAlchemyKeyRepository


def test_repository_detects_sqlite_duplicate_credential_violation() -> None:
    exc = IntegrityError(
        statement="INSERT INTO api_keys",
        params={},
        orig=Exception(
            "UNIQUE constraint failed: api_keys.provider, api_keys.credential_fingerprint"
        ),
    )

    assert SqlAlchemyKeyRepository._is_provider_credential_unique_violation(exc) is True
```

- [ ] **Step 7: Run focused tests**

Run:

```bash
python -m pytest tests/test_sqlite_local_runtime.py::test_credential_fingerprint_is_stable_for_key_order tests/test_sqlite_local_runtime.py::test_repository_detects_sqlite_duplicate_credential_violation -q
```

Expected: PASS.

- [ ] **Step 8: Run existing repository-adjacent tests**

Run: `python -m pytest tests/test_domain.py tests/test_api.py -q`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/infrastructure/db/models.py src/infrastructure/db/repository_impl.py src/infrastructure/db/bootstrap.py tests/test_sqlite_local_runtime.py
git commit -m "feat: use portable credential fingerprint"
```

---

### Task 3: Add SQLite Session and Bootstrap

**Files:**
- Create: `src/infrastructure/db/sqlite_session.py`
- Create: `src/infrastructure/db/sqlite_bootstrap.py`
- Test: `tests/test_sqlite_local_runtime.py`

- [ ] **Step 1: Add bootstrap test**

Append to `tests/test_sqlite_local_runtime.py`:

```python
from sqlalchemy import text

from infrastructure.db.sqlite_bootstrap import bootstrap_sqlite_database
from infrastructure.db.sqlite_session import create_sqlite_session_factory


async def test_sqlite_bootstrap_enables_wal_and_creates_schema(tmp_path) -> None:
    db_path = tmp_path / "keyflow.db"
    read_factory, write_factory = create_sqlite_session_factory(str(db_path))

    await bootstrap_sqlite_database(str(db_path), write_factory.kw["bind"])

    async with write_factory() as session:
        journal_mode = await session.execute(text("PRAGMA journal_mode"))
        tables = await session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        )

    assert journal_mode.scalar_one().lower() == "wal"
    assert "api_keys" in tables.scalars().all()

    await write_factory.kw["bind"].dispose()
```

- [ ] **Step 2: Run test and verify failure**

Run: `python -m pytest tests/test_sqlite_local_runtime.py::test_sqlite_bootstrap_enables_wal_and_creates_schema -q`

Expected: FAIL because `sqlite_session` and `sqlite_bootstrap` do not exist.

- [ ] **Step 3: Create SQLite session factory**

Create `src/infrastructure/db/sqlite_session.py`:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-13
@Description: SQLite 本地运行模式会话工厂
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def _sqlite_url(sqlite_path: str) -> str:
    path = Path(sqlite_path)
    if not path.is_absolute():
        path = path.resolve()
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def create_sqlite_session_factory(
    sqlite_path: str,
) -> tuple[async_sessionmaker[AsyncSession], async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        _sqlite_url(sqlite_path),
        connect_args={"timeout": 30},
        pool_pre_ping=True,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    return factory, factory
```

- [ ] **Step 4: Create SQLite bootstrap**

Create `src/infrastructure/db/sqlite_bootstrap.py`:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-13
@Description: SQLite 本地运行模式数据库初始化
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from infrastructure.db.models import Base


async def bootstrap_sqlite_database(sqlite_path: str, write_engine) -> None:
    Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    async with write_engine.begin() as connection:
        await connection.execute(text("PRAGMA journal_mode=WAL"))
        await connection.execute(text("PRAGMA foreign_keys=ON"))
        await connection.execute(text("PRAGMA busy_timeout=30000"))
        await connection.run_sync(Base.metadata.create_all)
```

- [ ] **Step 5: Run bootstrap test**

Run: `python -m pytest tests/test_sqlite_local_runtime.py::test_sqlite_bootstrap_enables_wal_and_creates_schema -q`

Expected: PASS.

- [ ] **Step 6: Add SQLite duplicate credential integration test**

Append to `tests/test_sqlite_local_runtime.py`:

```python
import pytest

from domain.entities.api_key import ApiKey
from domain.exceptions.domain_exceptions import DuplicateCredentialError
from domain.value_objects.key_status import KeyStatus
from infrastructure.db.repository_impl import SqlAlchemyKeyRepository


async def test_sqlite_repository_maps_duplicate_credential_to_domain_error(tmp_path) -> None:
    db_path = tmp_path / "keyflow.db"
    read_factory, write_factory = create_sqlite_session_factory(str(db_path))
    await bootstrap_sqlite_database(str(db_path), write_factory.kw["bind"])
    repository = SqlAlchemyKeyRepository(read_factory, write_factory)

    first = ApiKey(
        id="key-1",
        provider="openai",
        credential={"api_key": "sk-same"},
        status=KeyStatus.AVAILABLE,
    )
    duplicate = ApiKey(
        id="key-2",
        provider="openai",
        credential={"api_key": "sk-same"},
        status=KeyStatus.AVAILABLE,
    )

    await repository.upsert_key(first)
    with pytest.raises(DuplicateCredentialError):
        await repository.upsert_key(duplicate)

    await write_factory.kw["bind"].dispose()
```

- [ ] **Step 7: Run SQLite repository integration test**

Run: `python -m pytest tests/test_sqlite_local_runtime.py::test_sqlite_repository_maps_duplicate_credential_to_domain_error -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/infrastructure/db/sqlite_session.py src/infrastructure/db/sqlite_bootstrap.py tests/test_sqlite_local_runtime.py
git commit -m "feat: add sqlite wal bootstrap"
```

---

### Task 4: Add SQLite Allocation Store

**Files:**
- Create: `src/infrastructure/cache/sqlite_key_cache.py`
- Test: `tests/test_sqlite_local_runtime.py`

- [ ] **Step 1: Add allocation behavior tests**

Append to `tests/test_sqlite_local_runtime.py`:

```python
from datetime import datetime, timedelta, timezone

from domain.entities.api_key import ApiKey
from domain.value_objects.key_status import KeyStatus
from infrastructure.cache.sqlite_key_cache import SqliteKeyCache
from infrastructure.db.repository_impl import SqlAlchemyKeyRepository


def _local_key(key_id: str, provider: str = "openai", status: KeyStatus = KeyStatus.AVAILABLE) -> ApiKey:
    return ApiKey(
        id=key_id,
        provider=provider,
        credential={"api_key": key_id},
        status=status,
        supported_models=["gpt-4o"],
    )


async def test_sqlite_allocation_uses_lease_until_release(tmp_path) -> None:
    db_path = tmp_path / "keyflow.db"
    read_factory, write_factory = create_sqlite_session_factory(str(db_path))
    await bootstrap_sqlite_database(str(db_path), write_factory.kw["bind"])
    repository = SqlAlchemyKeyRepository(read_factory, write_factory)
    cache = SqliteKeyCache(write_factory.kw["bind"])
    now = datetime.now(timezone.utc)

    await repository.upsert_key(_local_key("key-1"))
    await repository.upsert_key(_local_key("key-2"))

    first = await cache.allocate_key("openai", ["key-1", "key-2"], now, lease_seconds=10)
    second = await cache.allocate_key("openai", ["key-1", "key-2"], now, lease_seconds=10)
    await cache.release_key_lease("openai", "key-1")
    third = await cache.allocate_key("openai", ["key-1", "key-2"], now, lease_seconds=10)

    assert first == "key-1"
    assert second == "key-2"
    assert third == "key-1"

    await write_factory.kw["bind"].dispose()


async def test_sqlite_allocation_reuses_expired_lease(tmp_path) -> None:
    db_path = tmp_path / "keyflow.db"
    read_factory, write_factory = create_sqlite_session_factory(str(db_path))
    await bootstrap_sqlite_database(str(db_path), write_factory.kw["bind"])
    repository = SqlAlchemyKeyRepository(read_factory, write_factory)
    cache = SqliteKeyCache(write_factory.kw["bind"])
    now = datetime.now(timezone.utc)

    await repository.upsert_key(_local_key("key-1"))

    first = await cache.allocate_key("openai", ["key-1"], now, lease_seconds=2)
    second = await cache.allocate_key("openai", ["key-1"], now + timedelta(seconds=1), lease_seconds=2)
    third = await cache.allocate_key("openai", ["key-1"], now + timedelta(seconds=3), lease_seconds=2)

    assert first == "key-1"
    assert second is None
    assert third == "key-1"

    await write_factory.kw["bind"].dispose()
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_sqlite_local_runtime.py::test_sqlite_allocation_uses_lease_until_release tests/test_sqlite_local_runtime.py::test_sqlite_allocation_reuses_expired_lease -q`

Expected: FAIL because `SqliteKeyCache` does not exist.

- [ ] **Step 3: Create SQLite allocation store**

Create `src/infrastructure/cache/sqlite_key_cache.py`:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-13
@Description: SQLite 本地模式 Key 分配与租约存储
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from domain.entities.api_key import ApiKey
from domain.repositories.key_repository import KeyAllocationStore
from domain.value_objects.key_status import KeyStatus
from infrastructure.db.models import ApiKeyModel, KeyLeaseModel


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class SqliteKeyCache(KeyAllocationStore):
    def __init__(self, write_engine: AsyncEngine) -> None:
        self._write_engine = write_engine

    async def sync_key(self, key: ApiKey, score: float) -> None:
        return None

    async def remove_key(self, key_id: str, provider: str) -> None:
        async with self._write_engine.begin() as conn:
            await conn.execute(delete(KeyLeaseModel).where(KeyLeaseModel.key_id == key_id))

    async def allocate_key(
        self,
        provider: str,
        ordered_key_ids: list[str],
        now: datetime,
        lease_seconds: int = 2,
    ) -> str | None:
        if not ordered_key_ids:
            return None

        now = _utc(now)
        expires_at = now + timedelta(seconds=max(lease_seconds, 1))
        async with self._write_engine.connect() as conn:
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                await conn.execute(
                    delete(KeyLeaseModel).where(KeyLeaseModel.lease_until <= now)
                )
                for key_id in ordered_key_ids:
                    result = await conn.execute(
                        select(
                            ApiKeyModel.id,
                            ApiKeyModel.provider,
                            ApiKeyModel.status,
                            ApiKeyModel.cooldown_until,
                        ).where(ApiKeyModel.id == key_id)
                    )
                    key = result.mappings().first()
                    if key is None or key["provider"] != provider:
                        continue
                    if not self._is_usable(key["status"], key["cooldown_until"], now):
                        continue

                    lease_result = await conn.execute(
                        select(KeyLeaseModel.lease_until).where(KeyLeaseModel.key_id == key_id)
                    )
                    lease_until = lease_result.scalar_one_or_none()
                    if lease_until is not None and _utc(lease_until) > now:
                        continue

                    if lease_until is None:
                        await conn.execute(
                            insert(KeyLeaseModel).values(
                                key_id=key_id,
                                provider=provider,
                                lease_until=expires_at,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                    else:
                        await conn.execute(
                            update(KeyLeaseModel)
                            .where(KeyLeaseModel.key_id == key_id)
                            .values(
                                provider=provider,
                                lease_until=expires_at,
                                updated_at=now,
                            )
                        )
                    await conn.commit()
                    return key_id
                await conn.commit()
                return None
            except Exception:
                await conn.rollback()
                raise

    async def allocate_key_any_provider(
        self,
        ordered_keys: list[ApiKey],
        now: datetime,
        lease_seconds: int = 2,
    ) -> str | None:
        for key in ordered_keys:
            allocated_id = await self.allocate_key(
                key.provider,
                [key.id],
                now,
                lease_seconds=lease_seconds,
            )
            if allocated_id is not None:
                return allocated_id
        return None

    async def release_key_lease(self, provider: str, key_id: str) -> None:
        async with self._write_engine.begin() as conn:
            await conn.execute(
                delete(KeyLeaseModel)
                .where(KeyLeaseModel.provider == provider)
                .where(KeyLeaseModel.key_id == key_id)
            )

    @staticmethod
    def _is_usable(status: str, cooldown_until: datetime | None, now: datetime) -> bool:
        if status == KeyStatus.AVAILABLE.value:
            return True
        if status not in {KeyStatus.RATE_LIMITED.value, KeyStatus.COOLDOWN.value}:
            return False
        if cooldown_until is None:
            return False
        return _utc(cooldown_until) <= now
```

- [ ] **Step 4: Run allocation tests**

Run: `python -m pytest tests/test_sqlite_local_runtime.py::test_sqlite_allocation_uses_lease_until_release tests/test_sqlite_local_runtime.py::test_sqlite_allocation_reuses_expired_lease -q`

Expected: PASS.

- [ ] **Step 5: Add concurrency regression test**

Append to `tests/test_sqlite_local_runtime.py`:

```python
import asyncio


async def test_sqlite_allocation_concurrent_calls_do_not_duplicate_key(tmp_path) -> None:
    db_path = tmp_path / "keyflow.db"
    read_factory, write_factory = create_sqlite_session_factory(str(db_path))
    await bootstrap_sqlite_database(str(db_path), write_factory.kw["bind"])
    repository = SqlAlchemyKeyRepository(read_factory, write_factory)
    cache = SqliteKeyCache(write_factory.kw["bind"])
    now = datetime.now(timezone.utc)

    await repository.upsert_key(_local_key("key-1"))
    await repository.upsert_key(_local_key("key-2"))

    results = await asyncio.gather(
        *[
            cache.allocate_key("openai", ["key-1", "key-2"], now, lease_seconds=10)
            for _ in range(10)
        ]
    )

    allocated = [item for item in results if item is not None]
    assert sorted(allocated) == ["key-1", "key-2"]

    await write_factory.kw["bind"].dispose()
```

- [ ] **Step 6: Run concurrency test**

Run: `python -m pytest tests/test_sqlite_local_runtime.py::test_sqlite_allocation_concurrent_calls_do_not_duplicate_key -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/infrastructure/cache/sqlite_key_cache.py tests/test_sqlite_local_runtime.py
git commit -m "feat: add sqlite allocation leases"
```

---

### Task 5: Wire Runtime Mode in Container

**Files:**
- Modify: `src/container/container.py`
- Modify: `src/interfaces/api/app.py`
- Test: `tests/test_sqlite_local_runtime.py`

- [ ] **Step 1: Add container wiring test**

Append to `tests/test_sqlite_local_runtime.py`:

```python
from application.services.key_service import KeyService
from container.container import create_container
from infrastructure.cache.sqlite_key_cache import SqliteKeyCache
from infrastructure.config.settings import Settings
from infrastructure.db.repository_impl import SqlAlchemyKeyRepository


def test_local_container_uses_sqlite_allocation_store(tmp_path) -> None:
    settings = Settings(
        KEYFLOW_RUNTIME_MODE="local",
        LOCAL_SQLITE_PATH=str(tmp_path / "keyflow.db"),
    )

    container = create_container(settings)

    assert container.resolve(KeyService) is not None
    assert isinstance(container.resolve(SqliteKeyCache), SqliteKeyCache)
    assert isinstance(container.resolve(SqlAlchemyKeyRepository), SqlAlchemyKeyRepository)
```

- [ ] **Step 2: Run test and verify failure**

Run: `python -m pytest tests/test_sqlite_local_runtime.py::test_local_container_uses_sqlite_allocation_store -q`

Expected: FAIL because container always registers `RedisKeyCache`.

- [ ] **Step 3: Modify container imports**

Modify `src/container/container.py` imports:

```python
from infrastructure.cache.key_cache import RedisKeyCache
from infrastructure.cache.redis_client import create_redis_client
from infrastructure.cache.sqlite_key_cache import SqliteKeyCache
from infrastructure.db.session import create_session_factory
from infrastructure.db.sqlite_session import create_sqlite_session_factory
```

- [ ] **Step 4: Modify container factory**

Replace the current factory/session/cache setup in `create_container` with:

```python
    if settings.runtime_mode == "local":
        read_factory, write_factory = create_sqlite_session_factory(settings.local_sqlite_path)
        repository = SqlAlchemyKeyRepository(read_factory, write_factory)
        allocation_store = SqliteKeyCache(write_factory.kw["bind"])
    elif settings.runtime_mode == "dev":
        read_factory, write_factory = create_session_factory(
            settings.database_read_url,
            settings.database_write_url,
        )
        redis = create_redis_client(settings.redis_url)
        repository = SqlAlchemyKeyRepository(read_factory, write_factory)
        allocation_store = RedisKeyCache(redis)
    else:
        raise ValueError("KEYFLOW_RUNTIME_MODE must be one of: dev, local")
```

Remove the existing duplicate lines that recreate `repository` and `allocation_store` after the scorer/scheduler setup.

At registration time, keep:

```python
    container.register(SqlAlchemyKeyRepository, instance=repository)
```

Then register mode-specific cache:

```python
    if isinstance(allocation_store, RedisKeyCache):
        container.register(RedisKeyCache, instance=allocation_store)
    if isinstance(allocation_store, SqliteKeyCache):
        container.register(SqliteKeyCache, instance=allocation_store)
```

Keep:

```python
    container.register(KeyService, instance=service)
```

- [ ] **Step 5: Run container test**

Run: `python -m pytest tests/test_sqlite_local_runtime.py::test_local_container_uses_sqlite_allocation_store -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/container/container.py tests/test_sqlite_local_runtime.py
git commit -m "feat: wire local runtime container"
```

---

### Task 6: Bootstrap and Health Checks by Mode

**Files:**
- Modify: `src/interfaces/api/app.py`
- Modify: `src/interfaces/api/routes/health.py`
- Test: `tests/test_sqlite_local_runtime.py`

- [ ] **Step 1: Add local health endpoint test**

Append to `tests/test_sqlite_local_runtime.py`:

```python
from fastapi.testclient import TestClient

from interfaces.api.app import create_app


def test_local_health_endpoint_checks_database_without_redis(tmp_path) -> None:
    settings = Settings(
        KEYFLOW_RUNTIME_MODE="local",
        LOCAL_SQLITE_PATH=str(tmp_path / "keyflow.db"),
    )

    app = create_app(settings=settings)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {
            "app": {"status": "ok", "detail": None},
            "database": {"status": "ok", "detail": None},
        },
    }
```

- [ ] **Step 2: Run test and verify failure**

Run: `python -m pytest tests/test_sqlite_local_runtime.py::test_local_health_endpoint_checks_database_without_redis -q`

Expected: FAIL because app health checkers still expect Redis.

- [ ] **Step 3: Update app imports**

Modify `src/interfaces/api/app.py` imports:

```python
from infrastructure.cache.key_cache import RedisKeyCache
from infrastructure.cache.sqlite_key_cache import SqliteKeyCache
from infrastructure.db.bootstrap import bootstrap_write_database
from infrastructure.db.sqlite_bootstrap import bootstrap_sqlite_database
```

- [ ] **Step 4: Update lifespan bootstrap**

Inside `lifespan`, change the initial dependency resolution so it resolves only `SqlAlchemyKeyRepository`. Do not resolve `RedisKeyCache` before checking runtime mode; local mode does not register Redis.

Use this shape:

```python
    try:
        repository: SqlAlchemyKeyRepository = app.state.container.resolve(SqlAlchemyKeyRepository)
    except punq.MissingDependencyError as exc:
        logger.info(
            "event=lifespan_runtime_dependencies_missing source=api_startup error=%s",
            exc,
        )
        yield
        return

    write_engine = repository._write_factory.kw["bind"]
    read_engine = repository._read_factory.kw["bind"]
    redis_cache: RedisKeyCache | None = None

    if app.state.settings.runtime_mode == "local":
        await bootstrap_sqlite_database(
            app.state.settings.local_sqlite_path,
            write_engine,
        )
    else:
        redis_cache = app.state.container.resolve(RedisKeyCache)
        await bootstrap_write_database(
            app.state.settings.database_write_url,
            write_engine,
        )
```

In the `finally` block:

```python
        if redis_cache is not None:
            await redis_cache._redis.aclose()
        await write_engine.dispose()
        if read_engine is not write_engine:
            await read_engine.dispose()
```

- [ ] **Step 5: Update health check attachment**

In `attach_health_checkers`, resolve repository first. Register `database` for both modes, and only register Redis in `dev` mode:

```python
    async def check_database() -> tuple[bool, str | None]:
        try:
            async with repository._read_factory() as session:
                await session.execute(text("SELECT 1"))
            return True, None
        except Exception as exc:
            return False, str(exc)

    if container.resolve(Settings).runtime_mode == "local":
        app.state.health_checkers = {
            "app": check_app,
            "database": check_database,
        }
        return
```

For `dev`, keep `app`, `database`, and `redis`.

- [ ] **Step 6: Update health route ordering**

Modify `src/interfaces/api/routes/health.py` so it does not require Redis when Redis is not registered:

```python
DEFAULT_CHECK_ORDER = ("app", "database", "redis")


def _ordered_check_names(checkers: dict[str, Any]) -> list[str]:
    if not checkers:
        return list(DEFAULT_CHECK_ORDER)
    ordered = [name for name in DEFAULT_CHECK_ORDER if name in checkers]
    extra = sorted(name for name in checkers if name not in DEFAULT_CHECK_ORDER)
    return [*ordered, *extra]
```

Then replace:

```python
    for name in CHECK_ORDER:
```

with:

```python
    for name in _ordered_check_names(checkers):
```

This preserves the existing dev response order while allowing local mode to expose only `app` and `database`.

- [ ] **Step 7: Run health endpoint test**

Run: `python -m pytest tests/test_sqlite_local_runtime.py::test_local_health_endpoint_checks_database_without_redis -q`

Expected: PASS.

- [ ] **Step 8: Run API health tests**

Run: `python -m pytest tests/test_api.py -q`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/interfaces/api/app.py src/interfaces/api/routes/health.py tests/test_sqlite_local_runtime.py
git commit -m "feat: add local runtime bootstrap and health checks"
```

---

### Task 7: Add Single-Container Local Entrypoint

**Files:**
- Create: `scripts/run_local_container.sh`
- Create: `docker/local/docker-compose.yml`
- Test: manual Docker command

- [ ] **Step 1: Create local run script**

Create `scripts/run_local_container.sh`:

```sh
#!/usr/bin/env sh
set -eu

python - <<'PY'
import asyncio

from container.container import create_container
from infrastructure.config.settings import get_settings
from infrastructure.db.repository_impl import SqlAlchemyKeyRepository
from infrastructure.db.sqlite_bootstrap import bootstrap_sqlite_database


async def main() -> None:
    settings = get_settings()
    if settings.runtime_mode != "local":
        raise RuntimeError("run_local_container.sh requires KEYFLOW_RUNTIME_MODE=local")
    container = create_container(settings)
    repository = container.resolve(SqlAlchemyKeyRepository)
    await bootstrap_sqlite_database(
        settings.local_sqlite_path,
        repository._write_factory.kw["bind"],
    )
    await repository._write_factory.kw["bind"].dispose()


asyncio.run(main())
PY

python src/worker_main.py &
worker_pid="$!"

uvicorn main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --app-dir src \
  --workers "${UVICORN_WORKERS:-2}" &
api_pid="$!"

shutdown() {
  kill "$api_pid" "$worker_pid" 2>/dev/null || true
  wait "$api_pid" "$worker_pid" 2>/dev/null || true
}

trap shutdown INT TERM

set +e
wait "$api_pid"
status="$?"
shutdown
exit "$status"
```

- [ ] **Step 2: Create local compose file**

Create `docker/local/docker-compose.yml`:

```yaml
name: keyflow-local

services:
  keyflow:
    container_name: keyflow-local
    build:
      context: ../..
      dockerfile: docker/src/Dockerfile
    command: ["sh", "scripts/run_local_container.sh"]
    env_file:
      - ../../.env
    environment:
      KEYFLOW_RUNTIME_MODE: local
      LOCAL_SQLITE_PATH: ${LOCAL_SQLITE_PATH:-/data/keyflow.db}
      PORT: ${PORT:-8000}
      UVICORN_WORKERS: ${UVICORN_WORKERS:-2}
    volumes:
      - ../../${MODEL_ALIAS_CONFIG_PATH:-config/model_aliases.yaml}:/app/${MODEL_ALIAS_CONFIG_PATH:-config/model_aliases.yaml}:ro
      - keyflow-local-data:/data
    ports:
      - "${PORT:-8000}:${PORT:-8000}"
    healthcheck:
      test:
        [
          "CMD",
          "python",
          "-c",
          "import os,urllib.request; p=os.environ.get('PORT','8000'); urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=3)",
        ]
      interval: 15s
      timeout: 5s
      retries: 3

volumes:
  keyflow-local-data:
```

- [ ] **Step 3: Manual compose validation**

Run:

```bash
docker compose -f docker/local/docker-compose.yml up --build
```

Expected:
- SQLite schema is initialized before worker and API start.
- Container starts.
- API process starts with configured workers.
- `python src/worker_main.py` starts in the same container.
- `/health` returns `200` with `database` check and no Redis requirement.

- [ ] **Step 4: Commit**

```bash
git add scripts/run_local_container.sh docker/local/docker-compose.yml
git commit -m "feat: add local single-container entrypoint"
```

---

### Task 8: Dev PostgreSQL Bootstrap Validation

**Files:**
- Test: existing API startup tests and dev bootstrap validation command

- [ ] **Step 1: Run API tests after local mode wiring**

Run: `python -m pytest tests/test_api.py tests/test_domain.py -q`

Expected: PASS.

- [ ] **Step 2: Run dev bootstrap validation when PostgreSQL is available**

Run with PostgreSQL available:

```bash
docker compose -f docker/postgresql/docker-compose.yml up -d
python -m pytest tests/test_api.py::test_health_reports_ready_when_dependencies_ok -q
```

Expected: PASS when PostgreSQL is available. If PostgreSQL is not available in the current machine, record the command as not run in the implementation summary.

- [ ] **Step 3: Commit validation notes if docs changed**

```bash
git status --short
```

Expected: no code changes are required in this task. If implementation notes were added to docs during execution, commit those docs with `git commit -m "docs: record dev bootstrap validation"`.

---

### Task 9: End-to-End Local Runtime Test

**Files:**
- Modify: `tests/test_sqlite_local_runtime.py`
- Test: local API allocation flow

- [ ] **Step 1: Add service-level local allocation test**

Append to `tests/test_sqlite_local_runtime.py`:

```python
from application.services.key_service import KeyService
from datetime import datetime, timezone
from domain.entities.api_key import ApiKey
from domain.services.scheduler import KeyScheduler
from domain.services.scorer import KeyScorer
from domain.services.state_machine import KeyStateMachine
from domain.value_objects.key_status import KeyStatus
from tests.fakes import FakeProviderPlugin, build_provider_registry


async def test_local_runtime_service_allocates_and_reports_success(tmp_path) -> None:
    settings = Settings(
        KEYFLOW_RUNTIME_MODE="local",
        LOCAL_SQLITE_PATH=str(tmp_path / "keyflow.db"),
    )
    container = create_container(settings)
    repository = container.resolve(SqlAlchemyKeyRepository)
    cache = container.resolve(SqliteKeyCache)
    await bootstrap_sqlite_database(str(tmp_path / "keyflow.db"), repository._write_factory.kw["bind"])
    scorer = KeyScorer()
    service = KeyService(
        repository,
        cache,
        KeyScheduler(scorer),
        scorer,
        KeyStateMachine(),
        build_provider_registry(FakeProviderPlugin("openai", models=["gpt-4o"])),
    )

    key = await repository.upsert_key(
        ApiKey(
            id="key-1",
            provider="openai",
            credential={"api_key": "sk-test"},
            status=KeyStatus.AVAILABLE,
            supported_models=["gpt-4o"],
        )
    )
    now = datetime.now(timezone.utc)
    await cache.sync_key(key, scorer.score(key, now))

    selected = await service.allocate_key("openai", "gpt-4o")
    reported = await service.report_success(selected.key.id, tokens_used=12)

    assert selected.key.id == key.id
    assert reported.success_count == 1
    assert reported.quota_used == 12

    await repository._write_factory.kw["bind"].dispose()
```

- [ ] **Step 2: Run local runtime tests**

Run: `python -m pytest tests/test_sqlite_local_runtime.py -q`

Expected: PASS.

- [ ] **Step 3: Run core suite**

Run:

```bash
python -m pytest tests/test_cache.py tests/test_domain.py tests/test_api.py tests/test_sqlite_local_runtime.py -q
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_sqlite_local_runtime.py
git commit -m "test: cover local sqlite runtime flow"
```

---

## Self-Review Checklist

- Spec coverage:
  - `local` mode without Redis/PostgreSQL: covered by Tasks 1, 3, 5, 6, 7.
  - Credential fingerprint model/repository/PostgreSQL bootstrap consistency: covered atomically in Task 2.
  - uvicorn multi-process runtime shape: covered by the dedicated local compose and pre-start SQLite bootstrap in Task 7.
  - Allocation lease concurrency: covered by SQLite WAL setup, `BEGIN IMMEDIATE`, and concurrent allocation tests in Tasks 3 and 4.
  - API + worker in one container: covered by Task 7.
  - Existing `dev` mode preserved: covered by Tasks 5, 6, 8 and existing test runs.
  - No unsupported mixed backends: covered by single `KEYFLOW_RUNTIME_MODE` branch in Task 5.
  - Task 9 deliberately avoids pending validation and tests only the local allocation/reporting runtime loop.
- Placeholder scan:
  - No `TBD`, `TODO`, or unspecified implementation steps remain.
- Type consistency:
  - `Settings.runtime_mode`, `Settings.local_sqlite_path`, `SqliteKeyCache`, `KeyLeaseModel`, and `credential_fingerprint` are introduced before dependent tasks use them.
