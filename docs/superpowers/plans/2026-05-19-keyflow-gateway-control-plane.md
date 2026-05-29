# KeyFlow Gateway Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an independent KeyFlow gateway control-plane service that lets ai_router manage credentials across registered local-mode KeyFlow child nodes.

**Architecture:** Add a separate FastAPI gateway app under `src/gateway` with its own SQLite-backed node repository, node registration/heartbeat APIs, node capability aggregation, and management request forwarding. Extend the existing child KeyFlow app with an optional background gateway client that registers and heartbeats without affecting local allocate/report/admin behavior.

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy asyncio, SQLite/aiosqlite, httpx, pytest.

**Repository rule for this project:** Do not run `git add`, `git commit`, or any git history operation unless the user explicitly asks for it.

---

## File Structure

- Create: `src/gateway/__init__.py`
  - Gateway package marker.
- Create: `src/gateway/config.py`
  - Gateway-only settings loaded from `.env.gateway`. The gateway env file reuses project-level
    names such as `APP_NAME`, `APP_VERSION`, `API_PREFIX`, `KEYFLOW_RUNTIME_MODE`, and
    `LOCAL_SQLITE_PATH` because it is deployed as a separate service.
- Create: `src/gateway/models.py`
  - SQLAlchemy model for `gateway_nodes`.
- Create: `src/gateway/schemas.py`
  - Pydantic request/response models and node status literals.
- Create: `src/gateway/repository.py`
  - `GatewayNodeRepository` protocol and `SQLiteGatewayNodeRepository` implementation.
- Create: `src/gateway/url_validation.py`
  - Normalize and validate node `base_url` origins.
- Create: `src/gateway/node_client.py`
  - HTTP client for child node health/provider/admin forwarding.
- Create: `src/gateway/service.py`
  - Gateway business logic: register, heartbeat, node list, update, capabilities, forwarding.
- Create: `src/gateway/app.py`
  - FastAPI app factory and lifespan bootstrap for gateway SQLite.
- Create: `src/gateway/main.py`
  - Uvicorn import target for the gateway container.
- Create: `src/interfaces/workers/gateway_client.py`
  - Optional child-node background client for registration and heartbeat.
- Modify: `src/infrastructure/config/settings.py`
  - Add child-node gateway client environment settings.
- Modify: `src/interfaces/api/app.py`
  - Start/stop the optional child-node gateway client during existing KeyFlow API lifespan.
- Modify: `.env.example`
  - Document gateway service settings and child-node gateway client settings.
- Create: `tests/test_gateway_control_plane.py`
  - Gateway API, repository, capabilities, forwarding, and auth tests.
- Create: `tests/test_child_gateway_client.py`
  - Child-node gateway client enable/disable, retry, heartbeat, and re-register tests.

---

### Task 1: Add Gateway Settings And URL Validation

**Files:**
- Create: `src/gateway/__init__.py`
- Create: `src/gateway/config.py`
- Create: `src/gateway/url_validation.py`
- Test: `tests/test_gateway_control_plane.py`

- [ ] **Step 1: Write failing settings and URL tests**

Create `tests/test_gateway_control_plane.py` with:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: KeyFlow gateway 控制面测试
"""
from __future__ import annotations

import pytest

from gateway.config import GatewaySettings
from gateway.url_validation import normalize_node_base_url


def test_gateway_settings_defaults_to_local_sqlite() -> None:
    settings = GatewaySettings(_env_file=None)

    assert settings.runtime_mode == "local"
    assert settings.sqlite_path == "data/keyflow_gateway.db"
    assert settings.api_prefix == "/api/gateway"
    assert settings.node_probe_cache_seconds == 15


def test_normalize_node_base_url_accepts_origin_and_trims_slash() -> None:
    assert normalize_node_base_url("http://keyflow-node-01:8000/") == "http://keyflow-node-01:8000"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "ftp://keyflow-node-01:8000",
        "http:///missing-host",
        "http://keyflow-node-01:8000/api",
        "http://keyflow-node-01:8000?x=1",
        "http://keyflow-node-01:8000#frag",
    ],
)
def test_normalize_node_base_url_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_node_base_url(value)
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_gateway_control_plane.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'gateway'`.

- [ ] **Step 3: Create gateway package and settings**

Create `src/gateway/__init__.py`:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: KeyFlow gateway 控制面包
"""
```

Create `src/gateway/config.py`:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: KeyFlow gateway 控制面配置
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env.gateway", env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="KeyFlow Gateway", alias="APP_NAME")
    app_version: str = Field(default="0.1.0", alias="APP_VERSION")
    api_prefix: str = Field(default="/api/gateway", alias="API_PREFIX")
    runtime_mode: str = Field(default="local", alias="KEYFLOW_RUNTIME_MODE")
    sqlite_path: str = Field(default="data/keyflow_gateway.db", alias="LOCAL_SQLITE_PATH")
    internal_key: str = Field(default="dev-gateway-internal-key", alias="GATEWAY_INTERNAL_KEY")
    register_key: str = Field(default="dev-gateway-register-key", alias="GATEWAY_REGISTER_KEY")
    heartbeat_timeout_seconds: int = Field(default=90, alias="GATEWAY_HEARTBEAT_TIMEOUT_SECONDS")
    node_http_connect_timeout_seconds: float = Field(default=1.0, alias="GATEWAY_NODE_HTTP_CONNECT_TIMEOUT_SECONDS")
    node_http_read_timeout_seconds: float = Field(default=5.0, alias="GATEWAY_NODE_HTTP_READ_TIMEOUT_SECONDS")
    node_probe_cache_seconds: int = Field(default=15, alias="GATEWAY_NODE_PROBE_CACHE_SECONDS")


@lru_cache(maxsize=1)
def get_gateway_settings() -> GatewaySettings:
    return GatewaySettings()
```

Datetime rule for all gateway SQLite fields: store naive UTC datetimes. Use `datetime.utcnow()`
for writes and compare only naive UTC datetimes in gateway code. Do not mix aware UTC datetimes
with SQLite-loaded datetimes.

- [ ] **Step 4: Create URL normalizer**

Create `src/gateway/url_validation.py`:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: KeyFlow gateway 子节点地址校验
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def normalize_node_base_url(value: str) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("base_url scheme must be http or https")
    if not parsed.netloc:
        raise ValueError("base_url host is required")
    if parsed.path not in {"", "/"}:
        raise ValueError("base_url must be an origin without path")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not include query or fragment")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")
```

- [ ] **Step 5: Run tests and verify pass**

Run: `python -m pytest tests/test_gateway_control_plane.py -q`

Expected: PASS.

---

### Task 2: Add Gateway Node Model And Repository

**Files:**
- Create: `src/gateway/models.py`
- Create: `src/gateway/repository.py`
- Modify: `tests/test_gateway_control_plane.py`

- [ ] **Step 1: Add failing repository tests**

Append to `tests/test_gateway_control_plane.py`:

```python
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from gateway.models import GatewayBase
from gateway.repository import GatewayNodeCreate, GatewayNodeRepository, GatewayNodeUpdate, SQLiteGatewayNodeRepository


async def build_gateway_repository(tmp_path) -> GatewayNodeRepository:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gateway.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(GatewayBase.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return SQLiteGatewayNodeRepository(session_factory)


@pytest.mark.anyio
async def test_repository_registers_and_updates_node_without_reenabling(tmp_path) -> None:
    repo = await build_gateway_repository(tmp_path)

    created = await repo.register_node(
        GatewayNodeCreate(
            node_id="node-1",
            display_name="Node 1",
            base_url="http://node-1:8000",
            internal_key="secret-1",
            tags=["a"],
            version="1.0",
        )
    )
    await repo.update_node("node-1", GatewayNodeUpdate(enabled=False))
    updated = await repo.register_node(
        GatewayNodeCreate(
            node_id="node-1",
            display_name="Node 1B",
            base_url="http://node-1b:8000",
            internal_key="secret-2",
            tags=["b"],
            version="1.1",
        )
    )

    assert created.node_id == "node-1"
    assert updated.display_name == "Node 1B"
    assert updated.base_url == "http://node-1b:8000"
    assert updated.internal_key == "secret-2"
    assert updated.tags == ["b"]
    assert updated.version == "1.1"
    assert updated.enabled is False
    assert updated.registered_at is not None


@pytest.mark.anyio
async def test_repository_heartbeat_and_probe_fields_are_separate(tmp_path) -> None:
    repo = await build_gateway_repository(tmp_path)
    await repo.register_node(
        GatewayNodeCreate(
            node_id="node-1",
            display_name="Node 1",
            base_url="http://node-1:8000",
            internal_key="secret",
            tags=[],
            version="1.0",
        )
    )

    heartbeat = await repo.record_heartbeat("node-1", runtime_status="running", version="1.1")
    probed = await repo.record_probe("node-1", status="healthy", error=None)

    assert heartbeat is not None
    assert heartbeat.last_heartbeat_at is not None
    assert heartbeat.last_runtime_status == "running"
    assert heartbeat.last_probe_at is None
    assert probed is not None
    assert probed.last_heartbeat_at == heartbeat.last_heartbeat_at
    assert probed.last_probe_status == "healthy"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_gateway_control_plane.py -q`

Expected: FAIL with `ModuleNotFoundError` for `gateway.models` or `gateway.repository`.

- [ ] **Step 3: Create SQLAlchemy model**

Create `src/gateway/models.py`:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: KeyFlow gateway SQLite 数据模型
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class GatewayBase(DeclarativeBase):
    pass


class GatewayNodeModel(GatewayBase):
    __tablename__ = "gateway_nodes"

    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    internal_key: Mapped[str] = mapped_column(Text, nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_runtime_status: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_probe_status: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    last_probe_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
```

- [ ] **Step 4: Create repository**

Create `src/gateway/repository.py`:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: KeyFlow gateway 节点仓储
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.models import GatewayNodeModel

ProbeStatus = Literal["unknown", "healthy", "degraded", "unreachable", "timeout", "disabled"]


def utc_now() -> datetime:
    return datetime.utcnow()


@dataclass(frozen=True)
class GatewayNode:
    node_id: str
    display_name: str
    base_url: str
    internal_key: str
    tags: list[str]
    enabled: bool
    version: str | None
    registered_at: datetime | None
    last_heartbeat_at: datetime | None
    last_runtime_status: str | None
    last_probe_at: datetime | None
    last_probe_status: str
    last_probe_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class GatewayNodeCreate:
    node_id: str
    display_name: str
    base_url: str
    internal_key: str
    tags: list[str]
    version: str | None


@dataclass(frozen=True)
class GatewayNodeUpdate:
    display_name: str | None = None
    tags: list[str] | None = None
    enabled: bool | None = None


class GatewayNodeRepository(Protocol):
    async def get_node(self, node_id: str) -> GatewayNode | None:
        raise NotImplementedError

    async def list_nodes(self) -> list[GatewayNode]:
        raise NotImplementedError

    async def register_node(self, payload: GatewayNodeCreate) -> GatewayNode:
        raise NotImplementedError

    async def update_node(self, node_id: str, payload: GatewayNodeUpdate) -> GatewayNode | None:
        raise NotImplementedError

    async def record_heartbeat(self, node_id: str, runtime_status: str | None, version: str | None) -> GatewayNode | None:
        raise NotImplementedError

    async def record_probe(self, node_id: str, status: ProbeStatus, error: str | None) -> GatewayNode | None:
        raise NotImplementedError


class SQLiteGatewayNodeRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _to_entity(model: GatewayNodeModel) -> GatewayNode:
        return GatewayNode(
            node_id=model.node_id,
            display_name=model.display_name,
            base_url=model.base_url,
            internal_key=model.internal_key,
            tags=json.loads(model.tags_json),
            enabled=model.enabled,
            version=model.version,
            registered_at=model.registered_at,
            last_heartbeat_at=model.last_heartbeat_at,
            last_runtime_status=model.last_runtime_status,
            last_probe_at=model.last_probe_at,
            last_probe_status=model.last_probe_status,
            last_probe_error=model.last_probe_error,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def get_node(self, node_id: str) -> GatewayNode | None:
        async with self._session_factory() as session:
            model = await session.get(GatewayNodeModel, node_id)
            return None if model is None else self._to_entity(model)

    async def list_nodes(self) -> list[GatewayNode]:
        async with self._session_factory() as session:
            result = await session.execute(select(GatewayNodeModel).order_by(GatewayNodeModel.node_id))
            return [self._to_entity(model) for model in result.scalars()]

    async def register_node(self, payload: GatewayNodeCreate) -> GatewayNode:
        now = utc_now()
        async with self._session_factory() as session:
            model = await session.get(GatewayNodeModel, payload.node_id)
            if model is None:
                model = GatewayNodeModel(
                    node_id=payload.node_id,
                    display_name=payload.display_name,
                    base_url=payload.base_url,
                    internal_key=payload.internal_key,
                    tags_json=json.dumps(payload.tags),
                    enabled=True,
                    version=payload.version,
                    registered_at=now,
                    last_probe_status="unknown",
                    created_at=now,
                    updated_at=now,
                )
                session.add(model)
            else:
                model.display_name = payload.display_name
                model.base_url = payload.base_url
                model.internal_key = payload.internal_key
                model.tags_json = json.dumps(payload.tags)
                model.version = payload.version
                model.registered_at = now
                model.updated_at = now
            await session.commit()
            await session.refresh(model)
            return self._to_entity(model)

    async def update_node(self, node_id: str, payload: GatewayNodeUpdate) -> GatewayNode | None:
        async with self._session_factory() as session:
            model = await session.get(GatewayNodeModel, node_id)
            if model is None:
                return None
            if payload.display_name is not None:
                model.display_name = payload.display_name
            if payload.tags is not None:
                model.tags_json = json.dumps(payload.tags)
            if payload.enabled is not None:
                model.enabled = payload.enabled
            model.updated_at = utc_now()
            await session.commit()
            await session.refresh(model)
            return self._to_entity(model)

    async def record_heartbeat(self, node_id: str, runtime_status: str | None, version: str | None) -> GatewayNode | None:
        async with self._session_factory() as session:
            model = await session.get(GatewayNodeModel, node_id)
            if model is None:
                return None
            now = utc_now()
            model.last_heartbeat_at = now
            model.last_runtime_status = runtime_status
            model.version = version or model.version
            model.updated_at = now
            await session.commit()
            await session.refresh(model)
            return self._to_entity(model)

    async def record_probe(self, node_id: str, status: ProbeStatus, error: str | None) -> GatewayNode | None:
        async with self._session_factory() as session:
            model = await session.get(GatewayNodeModel, node_id)
            if model is None:
                return None
            now = utc_now()
            model.last_probe_at = now
            model.last_probe_status = status
            model.last_probe_error = error
            model.updated_at = now
            await session.commit()
            await session.refresh(model)
            return self._to_entity(model)
```

- [ ] **Step 5: Run tests and verify pass**

Run: `python -m pytest tests/test_gateway_control_plane.py -q`

Expected: PASS.

---

### Task 3: Build Gateway Schemas, Auth, And App Bootstrap

**Files:**
- Create: `src/gateway/schemas.py`
- Create: `src/gateway/app.py`
- Create: `src/gateway/main.py`
- Modify: `tests/test_gateway_control_plane.py`

- [ ] **Step 1: Add failing API auth and registration tests**

Append:

```python
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from gateway.app import create_gateway_app
from gateway.config import GatewaySettings


@contextmanager
def build_gateway_client(db_dir: Path, node_transport: httpx.AsyncBaseTransport | None = None) -> Iterator[TestClient]:
    settings = GatewaySettings(
        LOCAL_SQLITE_PATH=str(db_dir / "gateway.db"),
        GATEWAY_INTERNAL_KEY="gateway-admin",
        GATEWAY_REGISTER_KEY="gateway-register",
        _env_file=None,
    )
    app = create_gateway_app(settings=settings, node_transport=node_transport)
    with TestClient(app) as client:
        yield client


def test_gateway_rejects_invalid_register_key(tmp_path) -> None:
    with build_gateway_client(tmp_path) as client:

        response = client.post(
            "/api/gateway/nodes/register",
            headers={"X-Gateway-Register-Key": "wrong"},
            json={
                "node_id": "node-1",
                "display_name": "Node 1",
                "base_url": "http://node-1:8000",
                "internal_key": "secret",
                "tags": [],
                "version": "1.0",
            },
        )

        assert response.status_code == 401
        assert response.json() == {"detail": "invalid gateway register key"}


def test_gateway_registers_and_lists_node_without_secrets(tmp_path) -> None:
    with build_gateway_client(tmp_path) as client:

        response = client.post(
            "/api/gateway/nodes/register",
            headers={"X-Gateway-Register-Key": "gateway-register"},
            json={
                "node_id": "node-1",
                "display_name": "Node 1",
                "base_url": "http://node-1:8000/",
                "internal_key": "secret",
                "tags": ["edge"],
                "version": "1.0",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "registered"

        list_response = client.get("/api/gateway/nodes", headers={"X-Gateway-Internal-Key": "gateway-admin"})
        assert list_response.status_code == 200
        payload = list_response.json()
        assert payload[0]["node_id"] == "node-1"
        assert payload[0]["status"] == "unknown"
        assert payload[0]["has_internal_key"] is True
        assert "internal_key" not in payload[0]
        assert "base_url" not in payload[0]


def test_gateway_register_rejects_invalid_base_url_with_422(tmp_path) -> None:
    with build_gateway_client(tmp_path) as client:

        response = client.post(
            "/api/gateway/nodes/register",
            headers={"X-Gateway-Register-Key": "gateway-register"},
            json={
                "node_id": "node-1",
                "display_name": "Node 1",
                "base_url": "http://node-1:8000/api",
                "internal_key": "secret",
                "tags": [],
                "version": "1.0",
            },
        )

        assert response.status_code == 422
        assert response.json() == {"detail": "base_url must be an origin without path"}
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_gateway_control_plane.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'gateway.app'`.

- [ ] **Step 3: Create schemas**

Create `src/gateway/schemas.py` with request and response models:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: KeyFlow gateway API 数据结构
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

NodeListStatus = Literal["unknown", "online", "stale", "disabled"]
ProbeStatus = Literal["unknown", "healthy", "degraded", "unreachable", "timeout", "disabled"]


class RegisterNodeRequest(BaseModel):
    node_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    internal_key: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    version: str | None = None


class HeartbeatRequest(BaseModel):
    version: str | None = None
    runtime_status: str | None = None


class UpdateNodeRequest(BaseModel):
    display_name: str | None = None
    tags: list[str] | None = None
    enabled: bool | None = None


class NodeRegistrationResponse(BaseModel):
    status: Literal["registered"]
    node_id: str


class GatewayNodeResponse(BaseModel):
    node_id: str
    display_name: str
    tags: list[str]
    enabled: bool
    status: NodeListStatus
    registered_at: datetime | None
    last_heartbeat_at: datetime | None
    last_runtime_status: str | None
    last_probe_status: str
    last_probe_at: datetime | None
    last_probe_error: str | None
    has_internal_key: bool


class ProviderCapabilityResponse(BaseModel):
    name: str
    available: bool
    auth_type: str
    model_source: str
    credential_hint: Any
    actions: list[str]


class NodeCapabilityResponse(BaseModel):
    node_id: str
    display_name: str
    status: ProbeStatus
    providers: list[ProviderCapabilityResponse]
    error: str | None


class CapabilitiesResponse(BaseModel):
    nodes: list[NodeCapabilityResponse]
```

- [ ] **Step 4: Create gateway app with register/list auth**

Create `src/gateway/app.py` with an app factory that bootstraps SQLite and exposes register/list routes. The implementation must use `normalize_node_base_url`, `SQLiteGatewayNodeRepository`, and `GatewaySettings`. `create_gateway_app()` must accept an optional `node_transport` argument so tests can inject an `httpx.ASGITransport` before lifespan creates the child-node client.

Use this concrete app skeleton:

```python
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from gateway.config import GatewaySettings, get_gateway_settings
from gateway.models import GatewayBase
from gateway.repository import GatewayNode, GatewayNodeCreate, GatewayNodeUpdate, SQLiteGatewayNodeRepository
from gateway.schemas import (
    GatewayNodeResponse,
    HeartbeatRequest,
    NodeRegistrationResponse,
    RegisterNodeRequest,
    UpdateNodeRequest,
)
from gateway.url_validation import normalize_node_base_url


def _utc_now() -> datetime:
    return datetime.utcnow()


def _node_status(node: GatewayNode, settings: GatewaySettings) -> str:
    if not node.enabled:
        return "disabled"
    if node.last_heartbeat_at is None:
        return "unknown"
    age = (_utc_now() - node.last_heartbeat_at).total_seconds()
    return "online" if age <= settings.heartbeat_timeout_seconds else "stale"


def _to_node_response(node: GatewayNode, settings: GatewaySettings) -> GatewayNodeResponse:
    return GatewayNodeResponse(
        node_id=node.node_id,
        display_name=node.display_name,
        tags=node.tags,
        enabled=node.enabled,
        status=_node_status(node, settings),
        registered_at=node.registered_at,
        last_heartbeat_at=node.last_heartbeat_at,
        last_runtime_status=node.last_runtime_status,
        last_probe_status=node.last_probe_status,
        last_probe_at=node.last_probe_at,
        last_probe_error=node.last_probe_error,
        has_internal_key=bool(node.internal_key),
    )


def create_gateway_app(
    settings: GatewaySettings | None = None,
    node_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    app_settings = settings or get_gateway_settings()
    db_path = Path(app_settings.sqlite_path)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    repo = SQLiteGatewayNodeRepository(session_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        async with engine.begin() as conn:
            await conn.run_sync(GatewayBase.metadata.create_all)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title=app_settings.app_name, version=app_settings.app_version, lifespan=lifespan)
    app.state.settings = app_settings
    app.state.repository = repo
    app.state.node_transport = node_transport
    router = APIRouter()
```

Then add the auth dependencies and routes below in the same function. The final two lines of `create_gateway_app()` must be:

```python
    app.include_router(router, prefix=app_settings.api_prefix)
    return app
```

Auth dependencies:

```python
async def require_internal_key(x_gateway_internal_key: str | None = Header(default=None)) -> None:
    if x_gateway_internal_key != app_settings.internal_key:
        raise HTTPException(status_code=401, detail="invalid gateway internal key")


async def require_register_key(x_gateway_register_key: str | None = Header(default=None)) -> None:
    if x_gateway_register_key != app_settings.register_key:
        raise HTTPException(status_code=401, detail="invalid gateway register key")
```

Use these concrete route shapes:

```python
@router.post("/nodes/register", response_model=NodeRegistrationResponse)
async def register_node(payload: RegisterNodeRequest, _: None = Depends(require_register_key)):
    try:
        normalized_url = normalize_node_base_url(payload.base_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    node = await repo.register_node(
        GatewayNodeCreate(
            node_id=payload.node_id,
            display_name=payload.display_name,
            base_url=normalized_url,
            internal_key=payload.internal_key,
            tags=payload.tags,
            version=payload.version,
        )
    )
    return NodeRegistrationResponse(status="registered", node_id=node.node_id)
```

```python
@router.get("/nodes", response_model=list[GatewayNodeResponse])
async def list_nodes(_: None = Depends(require_internal_key)):
    nodes = await repo.list_nodes()
    return [_to_node_response(node, app_settings) for node in nodes]
```

Create `src/gateway/main.py`:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: KeyFlow gateway FastAPI 入口
"""
from gateway.app import create_gateway_app

app = create_gateway_app()
```

- [ ] **Step 5: Run tests and verify pass**

Run: `python -m pytest tests/test_gateway_control_plane.py -q`

Expected: PASS.

---

### Task 4: Add Heartbeat And Node Metadata Update APIs

**Files:**
- Modify: `src/gateway/app.py`
- Modify: `tests/test_gateway_control_plane.py`

- [ ] **Step 1: Add failing heartbeat/update tests**

Append:

```python
def register_sample_node(client: TestClient) -> None:
    response = client.post(
        "/api/gateway/nodes/register",
        headers={"X-Gateway-Register-Key": "gateway-register"},
        json={
            "node_id": "node-1",
            "display_name": "Node 1",
            "base_url": "http://node-1:8000",
            "internal_key": "secret",
            "tags": ["edge"],
            "version": "1.0",
        },
    )
    assert response.status_code == 200


def test_gateway_heartbeat_updates_runtime_state(tmp_path) -> None:
    with build_gateway_client(tmp_path) as client:
        register_sample_node(client)

        response = client.post(
            "/api/gateway/nodes/node-1/heartbeat",
            headers={"X-Gateway-Register-Key": "gateway-register"},
            json={"runtime_status": "running", "version": "1.1"},
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        node = client.get("/api/gateway/nodes", headers={"X-Gateway-Internal-Key": "gateway-admin"}).json()[0]
        assert node["status"] == "online"
        assert node["last_runtime_status"] == "running"


def test_gateway_heartbeat_unknown_node_returns_404(tmp_path) -> None:
    with build_gateway_client(tmp_path) as client:

        response = client.post(
            "/api/gateway/nodes/missing/heartbeat",
            headers={"X-Gateway-Register-Key": "gateway-register"},
            json={"runtime_status": "running"},
        )

        assert response.status_code == 404
        assert response.json() == {"detail": "node_not_found"}


def test_gateway_update_node_changes_only_management_fields(tmp_path) -> None:
    with build_gateway_client(tmp_path) as client:
        register_sample_node(client)

        response = client.patch(
            "/api/gateway/nodes/node-1",
            headers={"X-Gateway-Internal-Key": "gateway-admin"},
            json={"display_name": "Renamed", "tags": ["new"], "enabled": False},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["display_name"] == "Renamed"
        assert payload["tags"] == ["new"]
        assert payload["enabled"] is False
        assert payload["status"] == "disabled"
        assert "base_url" not in payload
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_gateway_control_plane.py -q`

Expected: FAIL with 404 for heartbeat/update routes.

- [ ] **Step 3: Implement heartbeat and update routes**

Modify `src/gateway/app.py`:

```python
@router.post("/nodes/{node_id}/heartbeat")
async def heartbeat_node(node_id: str, payload: HeartbeatRequest, _: None = Depends(require_register_key)):
    node = await repo.record_heartbeat(node_id, payload.runtime_status, payload.version)
    if node is None:
        raise HTTPException(status_code=404, detail="node_not_found")
    return {"status": "ok"}


@router.patch("/nodes/{node_id}", response_model=GatewayNodeResponse)
async def update_node(node_id: str, payload: UpdateNodeRequest, _: None = Depends(require_internal_key)):
    node = await repo.update_node(
        node_id,
        GatewayNodeUpdate(display_name=payload.display_name, tags=payload.tags, enabled=payload.enabled),
    )
    if node is None:
        raise HTTPException(status_code=404, detail="node_not_found")
    return _to_node_response(node, app_settings)
```

- [ ] **Step 4: Run tests and verify pass**

Run: `python -m pytest tests/test_gateway_control_plane.py -q`

Expected: PASS.

---

### Task 5: Add Child Node HTTP Client And Capabilities Aggregation

**Files:**
- Create: `src/gateway/node_client.py`
- Create: `src/gateway/service.py`
- Modify: `src/gateway/app.py`
- Modify: `tests/test_gateway_control_plane.py`

- [ ] **Step 1: Add failing capabilities tests with fake child app**

Append:

```python
from fastapi import FastAPI, Header, HTTPException
import httpx


def build_child_app(expected_key: str = "secret") -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok", "checks": {"app": {"status": "ok", "detail": None}}}

    @app.get("/api/providers")
    async def providers(x_internal_key: str | None = Header(default=None)):
        if x_internal_key != expected_key:
            raise HTTPException(status_code=401, detail="invalid internal key")
        return [
            {
                "name": "openai",
                "description": "OpenAI",
                "auth_type": "bearer_api_key",
                "credential_hint": '{"api_key":"sk-redacted"}',
                "model_source": "remote",
                "available": True,
            }
        ]

    return app


def test_gateway_capabilities_uses_child_internal_key_and_returns_providers(tmp_path) -> None:
    child_app = build_child_app()
    transport = httpx.ASGITransport(app=child_app)
    with build_gateway_client(tmp_path, node_transport=transport) as client:
        register_sample_node(client)

        response = client.get("/api/gateway/capabilities", headers={"X-Gateway-Internal-Key": "gateway-admin"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["nodes"][0]["status"] == "healthy"
        assert payload["nodes"][0]["providers"][0]["name"] == "openai"
        assert "create_key" in payload["nodes"][0]["providers"][0]["actions"]


def test_gateway_capabilities_includes_disabled_nodes_without_probe(tmp_path) -> None:
    with build_gateway_client(tmp_path) as client:
        register_sample_node(client)
        client.patch(
            "/api/gateway/nodes/node-1",
            headers={"X-Gateway-Internal-Key": "gateway-admin"},
            json={"enabled": False},
        )

        response = client.get("/api/gateway/capabilities", headers={"X-Gateway-Internal-Key": "gateway-admin"})

        assert response.status_code == 200
        assert response.json()["nodes"] == [
            {"node_id": "node-1", "display_name": "Node 1", "status": "disabled", "providers": [], "error": None}
        ]


def test_gateway_capabilities_reuses_fresh_probe_cache_with_provider_cache(tmp_path) -> None:
    calls = {"health": 0, "providers": 0}
    child_app = FastAPI()

    @child_app.get("/health")
    async def health():
        calls["health"] += 1
        return {"status": "ok"}

    @child_app.get("/api/providers")
    async def providers(x_internal_key: str | None = Header(default=None)):
        calls["providers"] += 1
        if x_internal_key != "secret":
            raise HTTPException(status_code=401, detail="invalid internal key")
        return [
            {
                "name": "openai",
                "description": "OpenAI",
                "auth_type": "bearer_api_key",
                "credential_hint": '{"api_key":"sk-redacted"}',
                "model_source": "remote",
                "available": True,
            }
        ]

    transport = httpx.ASGITransport(app=child_app)
    with build_gateway_client(tmp_path, node_transport=transport) as client:
        register_sample_node(client)

        first = client.get("/api/gateway/capabilities", headers={"X-Gateway-Internal-Key": "gateway-admin"})
        second = client.get("/api/gateway/capabilities", headers={"X-Gateway-Internal-Key": "gateway-admin"})

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["nodes"][0]["providers"][0]["name"] == "openai"
        assert second.json()["nodes"][0]["providers"][0]["name"] == "openai"
        assert calls == {"health": 1, "providers": 1}


def test_gateway_capabilities_degrades_failed_node_without_failing_response(tmp_path) -> None:
    child_app = build_child_app(expected_key="secret")
    transport = httpx.ASGITransport(app=child_app)
    with build_gateway_client(tmp_path, node_transport=transport) as client:
        register_sample_node(client)
        response = client.post(
            "/api/gateway/nodes/register",
            headers={"X-Gateway-Register-Key": "gateway-register"},
            json={
                "node_id": "node-2",
                "display_name": "Node 2",
                "base_url": "http://node-2:8000",
                "internal_key": "wrong-secret",
                "tags": [],
                "version": "1.0",
            },
        )
        assert response.status_code == 200

        capabilities = client.get("/api/gateway/capabilities", headers={"X-Gateway-Internal-Key": "gateway-admin"})

        assert capabilities.status_code == 200
        nodes = {node["node_id"]: node for node in capabilities.json()["nodes"]}
        assert nodes["node-1"]["status"] == "healthy"
        assert nodes["node-1"]["providers"][0]["name"] == "openai"
        assert nodes["node-2"] == {
            "node_id": "node-2",
            "display_name": "Node 2",
            "status": "unreachable",
            "providers": [],
            "error": "node_unreachable",
        }


def test_gateway_capabilities_updates_probe_fields_without_changing_heartbeat(tmp_path) -> None:
    child_app = build_child_app()
    transport = httpx.ASGITransport(app=child_app)
    with build_gateway_client(tmp_path, node_transport=transport) as client:
        register_sample_node(client)
        heartbeat = client.post(
            "/api/gateway/nodes/node-1/heartbeat",
            headers={"X-Gateway-Register-Key": "gateway-register"},
            json={"runtime_status": "running", "version": "1.1"},
        )
        assert heartbeat.status_code == 200
        before = client.get("/api/gateway/nodes", headers={"X-Gateway-Internal-Key": "gateway-admin"}).json()[0]

        response = client.get("/api/gateway/capabilities", headers={"X-Gateway-Internal-Key": "gateway-admin"})
        after = client.get("/api/gateway/nodes", headers={"X-Gateway-Internal-Key": "gateway-admin"}).json()[0]

        assert response.status_code == 200
        assert after["last_heartbeat_at"] == before["last_heartbeat_at"]
        assert after["registered_at"] == before["registered_at"]
        assert after["last_runtime_status"] == "running"
        assert after["last_probe_status"] == "healthy"
        assert after["last_probe_at"] is not None
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_gateway_control_plane.py -q`

Expected: FAIL with 404 for `/api/gateway/capabilities`.

- [ ] **Step 3: Implement child node client**

Create `src/gateway/node_client.py`:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: KeyFlow gateway 子节点 HTTP 客户端
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class ChildProbeResult:
    status: str
    providers: list[dict[str, Any]]
    error: str | None


class ChildNodeClient:
    def __init__(self, connect_timeout: float, read_timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._timeout = httpx.Timeout(connect=connect_timeout, read=read_timeout, write=read_timeout, pool=connect_timeout)
        self._transport = transport

    async def fetch_capabilities(self, base_url: str, internal_key: str) -> ChildProbeResult:
        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=self._timeout, transport=self._transport) as client:
                health = await client.get("/health")
                if health.status_code == 503:
                    status = "degraded"
                elif health.is_success:
                    status = "healthy"
                else:
                    return ChildProbeResult(status="unreachable", providers=[], error="node_unreachable")
                providers = await client.get("/api/providers", headers={"X-Internal-Key": internal_key})
                providers.raise_for_status()
                return ChildProbeResult(status=status, providers=providers.json(), error=None)
        except httpx.TimeoutException:
            return ChildProbeResult(status="timeout", providers=[], error="node_timeout")
        except Exception:
            return ChildProbeResult(status="unreachable", providers=[], error="node_unreachable")
```

- [ ] **Step 4: Implement capabilities service/route**

Create `src/gateway/service.py`:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: KeyFlow gateway 控制面业务服务
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from gateway.config import GatewaySettings
from gateway.node_client import ChildNodeClient
from gateway.repository import GatewayNode, GatewayNodeRepository
from gateway.schemas import CapabilitiesResponse, NodeCapabilityResponse, ProviderCapabilityResponse

_ACTIONS = ["create_key", "list_keys", "update_key", "delete_key", "move_pool", "models", "explain"]


def _utc_now() -> datetime:
    return datetime.utcnow()


def _is_probe_cache_fresh(node: GatewayNode, settings: GatewaySettings) -> bool:
    if node.last_probe_at is None:
        return False
    return (_utc_now() - node.last_probe_at).total_seconds() <= settings.node_probe_cache_seconds


def _provider_response(payload: dict[str, Any]) -> ProviderCapabilityResponse:
    return ProviderCapabilityResponse(
        name=str(payload.get("name") or ""),
        available=bool(payload.get("available")),
        auth_type=str(payload.get("auth_type") or ""),
        model_source=str(payload.get("model_source") or ""),
        credential_hint=payload.get("credential_hint"),
        actions=list(_ACTIONS),
    )


class GatewayService:
    def __init__(
        self,
        repository: GatewayNodeRepository,
        child_client: ChildNodeClient,
        settings: GatewaySettings,
    ) -> None:
        self._repository = repository
        self._child_client = child_client
        self._settings = settings
        self._provider_cache: dict[str, list[dict[str, Any]]] = {}

    async def _capability_for_node(self, node: GatewayNode) -> NodeCapabilityResponse:
        if not node.enabled:
            return NodeCapabilityResponse(
                node_id=node.node_id,
                display_name=node.display_name,
                status="disabled",
                providers=[],
                error=None,
            )

        if _is_probe_cache_fresh(node, self._settings) and node.node_id in self._provider_cache:
            cached_providers = self._provider_cache.get(node.node_id, [])
            return NodeCapabilityResponse(
                node_id=node.node_id,
                display_name=node.display_name,
                status=node.last_probe_status,
                providers=[_provider_response(provider) for provider in cached_providers],
                error=node.last_probe_error,
            )

        result = await self._child_client.fetch_capabilities(node.base_url, node.internal_key)
        self._provider_cache[node.node_id] = result.providers
        await self._repository.record_probe(node.node_id, status=result.status, error=result.error)
        return NodeCapabilityResponse(
            node_id=node.node_id,
            display_name=node.display_name,
            status=result.status,
            providers=[_provider_response(provider) for provider in result.providers],
            error=result.error,
        )

    async def get_capabilities(self) -> CapabilitiesResponse:
        nodes = await self._repository.list_nodes()
        return CapabilitiesResponse(nodes=[await self._capability_for_node(node) for node in nodes])
```

This v1 cache stores provider details in memory only, not in SQLite. Restarting the gateway clears
the in-memory provider cache; the next stale or uncached capabilities request probes the child
node again.

Modify `src/gateway/app.py` to create `ChildNodeClient` inside `create_gateway_app()` using the `node_transport` argument:

Add these imports:

```python
from gateway.node_client import ChildNodeClient
from gateway.schemas import CapabilitiesResponse
from gateway.service import GatewayService
```

```python
child_client = ChildNodeClient(
    connect_timeout=app_settings.node_http_connect_timeout_seconds,
    read_timeout=app_settings.node_http_read_timeout_seconds,
    transport=node_transport,
)
gateway_service = GatewayService(repo, child_client, app_settings)
```

Then expose:

```python
@router.get("/capabilities", response_model=CapabilitiesResponse)
async def capabilities(_: None = Depends(require_internal_key)):
    return await gateway_service.get_capabilities()
```

- [ ] **Step 5: Run tests and verify pass**

Run: `python -m pytest tests/test_gateway_control_plane.py -q`

Expected: PASS.

---

### Task 6: Add Forwarded Credential Management Routes

**Files:**
- Modify: `src/gateway/node_client.py`
- Modify: `src/gateway/app.py`
- Modify: `tests/test_gateway_control_plane.py`

- [ ] **Step 1: Add failing forwarding tests**

Append these forwarding tests:

```python
def test_gateway_forwards_create_key_to_child_node(tmp_path) -> None:
    observed = {}
    child_app = FastAPI()

    @child_app.post("/api/providers/openai/keys")
    async def create_key(payload: dict, x_internal_key: str | None = Header(default=None)):
        observed["key"] = x_internal_key
        observed["payload"] = payload
        return {"status": "ok", "key_id": "key-1"}

    transport = httpx.ASGITransport(app=child_app)
    with build_gateway_client(tmp_path, node_transport=transport) as client:
        register_sample_node(client)

        response = client.post(
            "/api/gateway/nodes/node-1/providers/openai/keys",
            headers={"X-Gateway-Internal-Key": "gateway-admin"},
            json={"credential": {"api_key": "sk-new"}, "pool": "vip"},
        )

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "key_id": "key-1"}
        assert observed["key"] == "secret"
        assert observed["payload"] == {"credential": {"api_key": "sk-new"}, "pool": "vip"}


def test_gateway_forwarding_rejects_disabled_node(tmp_path) -> None:
    with build_gateway_client(tmp_path) as client:
        register_sample_node(client)
        client.patch(
            "/api/gateway/nodes/node-1",
            headers={"X-Gateway-Internal-Key": "gateway-admin"},
            json={"enabled": False},
        )

        response = client.get(
            "/api/gateway/nodes/node-1/providers/openai/keys",
            headers={"X-Gateway-Internal-Key": "gateway-admin"},
        )

        assert response.status_code == 409
        assert response.json() == {"detail": "node_disabled"}


def test_gateway_forwarding_requires_internal_key(tmp_path) -> None:
    with build_gateway_client(tmp_path) as client:
        register_sample_node(client)

        response = client.get("/api/gateway/nodes/node-1/providers/openai/keys")

        assert response.status_code == 401
        assert response.json() == {"detail": "invalid gateway internal key"}


def test_gateway_forwarding_preserves_child_business_error(tmp_path) -> None:
    child_app = FastAPI()

    @child_app.get("/api/keys/missing")
    async def get_missing_key(x_internal_key: str | None = Header(default=None)):
        assert x_internal_key == "secret"
        raise HTTPException(status_code=404, detail="key_not_found")

    transport = httpx.ASGITransport(app=child_app)
    with build_gateway_client(tmp_path, node_transport=transport) as client:
        register_sample_node(client)

        response = client.get(
            "/api/gateway/nodes/node-1/keys/missing",
            headers={"X-Gateway-Internal-Key": "gateway-admin"},
        )

        assert response.status_code == 404
        assert response.json() == {"detail": "key_not_found"}


def test_gateway_forwarding_maps_unreachable_node_to_503(tmp_path) -> None:
    class UnreachableTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection failed", request=request)

    with build_gateway_client(tmp_path, node_transport=UnreachableTransport()) as client:
        register_sample_node(client)

        response = client.get(
            "/api/gateway/nodes/node-1/providers/openai/keys",
            headers={"X-Gateway-Internal-Key": "gateway-admin"},
        )

        assert response.status_code == 503
        assert response.json() == {"detail": "node_unreachable"}


def test_gateway_forwarding_maps_timeout_to_504(tmp_path) -> None:
    class TimeoutTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timed out", request=request)

    with build_gateway_client(tmp_path, node_transport=TimeoutTransport()) as client:
        register_sample_node(client)

        response = client.get(
            "/api/gateway/nodes/node-1/providers/openai/keys",
            headers={"X-Gateway-Internal-Key": "gateway-admin"},
        )

        assert response.status_code == 504
        assert response.json() == {"detail": "node_timeout"}
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_gateway_control_plane.py -q`

Expected: FAIL with 404 for forwarded routes.

- [ ] **Step 3: Implement generic forwarder**

Add to `ChildNodeClient`:

```python
async def forward(self, base_url: str, internal_key: str, method: str, path: str, json_body: Any | None) -> httpx.Response:
    async with httpx.AsyncClient(base_url=base_url, timeout=self._timeout, transport=self._transport) as client:
        return await client.request(method, path, headers={"X-Internal-Key": internal_key}, json=json_body)
```

Ensure these imports exist in `src/gateway/app.py`:

```python
from typing import Any

from fastapi.responses import Response
```

Add helper in `src/gateway/app.py`:

```python
async def forward_to_node(node_id: str, method: str, path: str, body: Any | None):
    node = await repo.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="node_not_found")
    if not node.enabled:
        raise HTTPException(status_code=409, detail="node_disabled")
    try:
        response = await child_client.forward(node.base_url, node.internal_key, method, path, body)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="node_timeout")
    except Exception:
        raise HTTPException(status_code=503, detail="node_unreachable")
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type") or "application/json",
    )
```

Add these concrete routes:

```python
@router.post("/nodes/{node_id}/providers/{provider}/keys")
async def create_key(
    node_id: str,
    provider: str,
    payload: dict[str, Any],
    _: None = Depends(require_internal_key),
):
    return await forward_to_node(node_id, "POST", f"/api/providers/{provider}/keys", payload)


@router.get("/nodes/{node_id}/providers/{provider}/keys")
async def list_provider_keys(node_id: str, provider: str, _: None = Depends(require_internal_key)):
    return await forward_to_node(node_id, "GET", f"/api/providers/{provider}/keys", None)


@router.get("/nodes/{node_id}/keys/{key_id}")
async def get_key(node_id: str, key_id: str, _: None = Depends(require_internal_key)):
    return await forward_to_node(node_id, "GET", f"/api/keys/{key_id}", None)


@router.put("/nodes/{node_id}/keys/{key_id}")
async def update_key(
    node_id: str,
    key_id: str,
    payload: dict[str, Any],
    _: None = Depends(require_internal_key),
):
    return await forward_to_node(node_id, "PUT", f"/api/keys/{key_id}", payload)


@router.put("/nodes/{node_id}/keys/{key_id}/pool")
async def move_key_pool(
    node_id: str,
    key_id: str,
    payload: dict[str, Any],
    _: None = Depends(require_internal_key),
):
    return await forward_to_node(node_id, "PUT", f"/api/keys/{key_id}/pool", payload)


@router.delete("/nodes/{node_id}/keys/{key_id}")
async def delete_key(node_id: str, key_id: str, _: None = Depends(require_internal_key)):
    return await forward_to_node(node_id, "DELETE", f"/api/keys/{key_id}", None)


@router.get("/nodes/{node_id}/providers/{provider}/keys/{key_id}/models")
async def get_key_models(
    node_id: str,
    provider: str,
    key_id: str,
    _: None = Depends(require_internal_key),
):
    return await forward_to_node(node_id, "GET", f"/api/providers/{provider}/keys/{key_id}/models", None)


@router.get("/nodes/{node_id}/keys/{key_id}/explain")
async def explain_key(node_id: str, key_id: str, _: None = Depends(require_internal_key)):
    return await forward_to_node(node_id, "GET", f"/api/keys/{key_id}/explain", None)
```

- [ ] **Step 4: Run tests and verify pass**

Run: `python -m pytest tests/test_gateway_control_plane.py -q`

Expected: PASS.

---

### Task 7: Add Optional Child-Node Gateway Client

**Files:**
- Modify: `src/infrastructure/config/settings.py`
- Create: `src/interfaces/workers/gateway_client.py`
- Modify: `src/interfaces/api/app.py`
- Test: `tests/test_child_gateway_client.py`

- [ ] **Step 1: Write failing child gateway client tests**

Create `tests/test_child_gateway_client.py`:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: 子节点 gateway 注册客户端测试
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException

from infrastructure.config.settings import Settings
from interfaces.workers.gateway_client import GatewayClientConfig, build_gateway_client_config, run_gateway_client


def test_build_gateway_client_config_disabled_when_required_values_missing() -> None:
    settings = Settings(_env_file=None)

    assert build_gateway_client_config(settings) is None


def test_build_gateway_client_config_enabled_when_required_values_exist() -> None:
    settings = Settings(
        GATEWAY_URL="http://gateway:8000",
        GATEWAY_REGISTER_KEY="register",
        NODE_ID="node-1",
        NODE_PUBLIC_BASE_URL="http://node-1:8000",
        INTERNAL_API_KEY="child-secret",
        _env_file=None,
    )

    config = build_gateway_client_config(settings)

    assert config == GatewayClientConfig(
        gateway_url="http://gateway:8000",
        register_key="register",
        node_id="node-1",
        display_name="node-1",
        public_base_url="http://node-1:8000",
        internal_key="child-secret",
        tags=[],
        heartbeat_interval_seconds=30,
    )


@pytest.mark.anyio
async def test_run_gateway_client_registers_and_sends_heartbeat() -> None:
    calls: list[str] = []
    app = FastAPI()

    @app.post("/api/gateway/nodes/register")
    async def register(x_gateway_register_key: str | None = Header(default=None)):
        assert x_gateway_register_key == "register"
        calls.append("register")
        return {"status": "registered", "node_id": "node-1"}

    @app.post("/api/gateway/nodes/node-1/heartbeat")
    async def heartbeat(x_gateway_register_key: str | None = Header(default=None)):
        assert x_gateway_register_key == "register"
        calls.append("heartbeat")
        return {"status": "ok"}

    config = GatewayClientConfig(
        gateway_url="http://gateway",
        register_key="register",
        node_id="node-1",
        display_name="node-1",
        public_base_url="http://node-1:8000",
        internal_key="child-secret",
        tags=[],
        heartbeat_interval_seconds=0.02,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_gateway_client(config, stop, transport=httpx.ASGITransport(app=app))
    )
    await asyncio.sleep(0.05)
    stop.set()
    await task

    assert "register" in calls
    assert "heartbeat" in calls


@pytest.mark.anyio
async def test_run_gateway_client_reregisters_after_heartbeat_404() -> None:
    calls: list[str] = []
    app = FastAPI()

    @app.post("/api/gateway/nodes/register")
    async def register():
        calls.append("register")
        return {"status": "registered", "node_id": "node-1"}

    @app.post("/api/gateway/nodes/node-1/heartbeat")
    async def heartbeat():
        calls.append("heartbeat")
        raise HTTPException(status_code=404, detail="node_not_found")

    config = GatewayClientConfig(
        gateway_url="http://gateway",
        register_key="register",
        node_id="node-1",
        display_name="node-1",
        public_base_url="http://node-1:8000",
        internal_key="child-secret",
        tags=[],
        heartbeat_interval_seconds=0.02,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_gateway_client(config, stop, transport=httpx.ASGITransport(app=app))
    )
    await asyncio.sleep(0.05)
    stop.set()
    await task

    assert calls.count("register") >= 2
    assert "heartbeat" in calls
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_child_gateway_client.py -q`

Expected: FAIL because settings and gateway client do not exist.

- [ ] **Step 3: Add child-node settings**

Modify `src/infrastructure/config/settings.py` and add fields:

```python
gateway_url: str | None = Field(default=None, alias="GATEWAY_URL")
gateway_register_key: str | None = Field(default=None, alias="GATEWAY_REGISTER_KEY")
node_id: str | None = Field(default=None, alias="NODE_ID")
node_display_name: str | None = Field(default=None, alias="NODE_DISPLAY_NAME")
node_public_base_url: str | None = Field(default=None, alias="NODE_PUBLIC_BASE_URL")
node_tags: str | None = Field(default=None, alias="NODE_TAGS")
node_heartbeat_interval_seconds: int = Field(default=30, alias="NODE_HEARTBEAT_INTERVAL_SECONDS")
```

- [ ] **Step 4: Create gateway client worker**

Create `src/interfaces/workers/gateway_client.py` with:

```python
"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: 子节点 gateway 注册与心跳客户端
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from infrastructure.config.settings import Settings


@dataclass(frozen=True)
class GatewayClientConfig:
    gateway_url: str
    register_key: str
    node_id: str
    display_name: str
    public_base_url: str
    internal_key: str
    tags: list[str]
    heartbeat_interval_seconds: float


def build_gateway_client_config(settings: Settings) -> GatewayClientConfig | None:
    required = [
        getattr(settings, "gateway_url", None),
        getattr(settings, "gateway_register_key", None),
        getattr(settings, "node_id", None),
        getattr(settings, "node_public_base_url", None),
        getattr(settings, "internal_api_key", None),
    ]
    if any(not value for value in required):
        return None
    tags = [item.strip() for item in (getattr(settings, "node_tags", None) or "").split(",") if item.strip()]
    return GatewayClientConfig(
        gateway_url=str(getattr(settings, "gateway_url")).rstrip("/"),
        register_key=str(getattr(settings, "gateway_register_key")),
        node_id=str(getattr(settings, "node_id")),
        display_name=getattr(settings, "node_display_name", None) or str(getattr(settings, "node_id")),
        public_base_url=str(getattr(settings, "node_public_base_url")),
        internal_key=str(getattr(settings, "internal_api_key")),
        tags=tags,
        heartbeat_interval_seconds=getattr(settings, "node_heartbeat_interval_seconds", 30),
    )


async def register_node(client: httpx.AsyncClient, config: GatewayClientConfig) -> bool:
    response = await client.post(
        "/api/gateway/nodes/register",
        headers={"X-Gateway-Register-Key": config.register_key},
        json={
            "node_id": config.node_id,
            "display_name": config.display_name,
            "base_url": config.public_base_url,
            "internal_key": config.internal_key,
            "tags": config.tags,
            "version": None,
        },
    )
    return response.is_success


async def send_heartbeat(client: httpx.AsyncClient, config: GatewayClientConfig) -> str:
    response = await client.post(
        f"/api/gateway/nodes/{config.node_id}/heartbeat",
        headers={"X-Gateway-Register-Key": config.register_key},
        json={"runtime_status": "running", "version": None},
    )
    if response.status_code == 404:
        return "missing"
    return "ok" if response.is_success else "error"


async def run_gateway_client(
    config: GatewayClientConfig,
    stop_event: asyncio.Event,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    backoff_seconds = 1
    async with httpx.AsyncClient(base_url=config.gateway_url, timeout=5.0, transport=transport) as client:
        while not stop_event.is_set():
            try:
                registered = await register_node(client, config)
                if registered:
                    break
            except Exception:
                registered = False
            if not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff_seconds)
                except asyncio.TimeoutError:
                    pass
            backoff_seconds = min(backoff_seconds * 2, 60)
        while not stop_event.is_set():
            try:
                heartbeat_status = await send_heartbeat(client, config)
                if heartbeat_status == "missing":
                    await register_node(client, config)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(config.heartbeat_interval_seconds, 0.01))
            except asyncio.TimeoutError:
                pass
```

- [ ] **Step 5: Wire client into API lifespan**

Modify `src/interfaces/api/app.py`:

Update imports:

```python
from contextlib import asynccontextmanager, suppress

from interfaces.workers.gateway_client import build_gateway_client_config, run_gateway_client
```

Replace the current `lifespan()` body with this exact flow. The gateway client starts only after
runtime dependencies and database bootstrap succeed. If runtime dependencies are missing, the
existing early-return path stays non-blocking and does not start the gateway client.

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
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
    gateway_stop_event: asyncio.Event | None = None
    gateway_task: asyncio.Task | None = None

    if getattr(app.state.settings, "runtime_mode", "dev") == "local":
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

    gateway_config = build_gateway_client_config(app.state.settings)
    if gateway_config is not None:
        gateway_stop_event = asyncio.Event()
        gateway_task = asyncio.create_task(run_gateway_client(gateway_config, gateway_stop_event))

    try:
        yield
    finally:
        if gateway_stop_event is not None:
            gateway_stop_event.set()
        if gateway_task is not None:
            gateway_task.cancel()
            with suppress(asyncio.CancelledError):
                await gateway_task
        if redis_cache is not None:
            await redis_cache._redis.aclose()
        await write_engine.dispose()
        if read_engine is not write_engine:
            await read_engine.dispose()
```

- [ ] **Step 6: Run tests and verify pass**

Run: `python -m pytest tests/test_child_gateway_client.py tests/test_api.py::test_api_lifespan_does_not_start_background_loop -q`

Expected: PASS.

---

### Task 8: Update Environment Examples And Run Focused Regression

**Files:**
- Modify: `.env.example`
- Create: `.env.gateway.example`
- Test: focused pytest commands below.

- [ ] **Step 1: Keep child-node `.env.example` scoped to child-node settings**

Modify `.env.example` with only the optional child-node registration client section. Do not add
gateway service settings to `.env.example`; gateway service settings belong in `.env.gateway.example`.

```env
# Optional child-node registration client
GATEWAY_URL=
GATEWAY_REGISTER_KEY=
NODE_ID=
NODE_DISPLAY_NAME=
NODE_PUBLIC_BASE_URL=
NODE_TAGS=
NODE_HEARTBEAT_INTERVAL_SECONDS=30
```

Create `.env.gateway.example` with gateway service settings:

```env
APP_NAME=KeyFlow Gateway
APP_DESCRIPTION=KeyFlow gateway control-plane service.
APP_VERSION=0.1.0
PORT=8001
API_PREFIX=/api/gateway

# Gateway service runtime mode:
# - local: SQLite-backed gateway control plane
# - dev: reserved for future PostgreSQL-backed gateway mode
KEYFLOW_RUNTIME_MODE=local
LOCAL_SQLITE_PATH=data/keyflow_gateway.db
LOG_LEVEL=INFO

# Gateway control-plane auth
GATEWAY_INTERNAL_KEY=change-me-gateway-admin
GATEWAY_REGISTER_KEY=change-me-gateway-register

# Gateway node health/probe behavior
GATEWAY_HEARTBEAT_TIMEOUT_SECONDS=90
GATEWAY_NODE_HTTP_CONNECT_TIMEOUT_SECONDS=1
GATEWAY_NODE_HTTP_READ_TIMEOUT_SECONDS=5
GATEWAY_NODE_PROBE_CACHE_SECONDS=15
```

- [ ] **Step 2: Run gateway tests**

Run: `python -m pytest tests/test_gateway_control_plane.py tests/test_child_gateway_client.py -q`

Expected: PASS.

- [ ] **Step 3: Run API regression tests affected by lifespan/settings**

Run: `python -m pytest tests/test_api.py tests/test_sqlite_local_runtime.py -q`

Expected: PASS.

- [ ] **Step 4: Run full stable suite if environment allows**

Run: `python -m pytest tests/test_domain.py tests/test_key_pool.py tests/test_cache.py tests/test_plugin_registry.py tests/test_container_plugins.py tests/test_provider_plugins.py tests/test_gateway_control_plane.py tests/test_child_gateway_client.py tests/test_api.py -q`

Expected: PASS. If provider tests require optional local dependencies or external credentials, record the exact failing tests and error messages instead of hiding them.

---

## Self-Review Checklist

- Spec coverage:
  - Independent gateway service: Tasks 1-6.
  - SQLite node storage: Task 2.
  - Register/heartbeat: Tasks 3-4 and Task 7.
  - No runtime allocation through gateway: Task 6 only forwards management routes.
  - Child-node local-mode autonomy: Task 7 client is optional and non-blocking.
  - Node list hides base URL and internal key: Task 3 and Task 8 tests.
  - Capabilities probes `/health` and `/api/providers` with child `X-Internal-Key`: Task 5.
  - Disabled nodes included but not probed: Task 5.
  - Capabilities degrades a failed node without failing the whole response: Task 5.
  - Capabilities updates probe fields without changing registration or heartbeat fields: Task 5.
  - Probe cache and in-memory provider cache: Task 5.
  - Forwarded key management routes: Task 6.
  - Forwarding preserves child-node business errors: Task 6.
  - Forwarding maps unreachable and timeout failures to gateway-owned errors: Task 6.
- Placeholder scan:
  - The plan should not contain placeholder markers or unspecified implementation steps.
- Type consistency:
  - `GatewayNode`, `GatewayNodeCreate`, `GatewayNodeUpdate`, `GatewaySettings`, and schema field names match across tasks.
- Git:
  - No task includes `git add` or `git commit`; do not run git operations without explicit user approval.
