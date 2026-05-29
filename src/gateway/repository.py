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
