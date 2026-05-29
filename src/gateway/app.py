"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: KeyFlow gateway FastAPI 应用工厂
"""
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
from gateway.node_client import ChildNodeClient
from gateway.repository import GatewayNode, GatewayNodeCreate, GatewayNodeUpdate, SQLiteGatewayNodeRepository
from gateway.schemas import (
    CapabilitiesResponse,
    GatewayNodeResponse,
    HeartbeatRequest,
    NodeRegistrationResponse,
    RegisterNodeRequest,
    UpdateNodeRequest,
)
from gateway.service import GatewayService
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
    child_client = ChildNodeClient(
        connect_timeout=app_settings.node_http_connect_timeout_seconds,
        read_timeout=app_settings.node_http_read_timeout_seconds,
        transport=node_transport,
    )
    gateway_service = GatewayService(repo, child_client, app_settings)

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

    async def require_internal_key(x_gateway_internal_key: str | None = Header(default=None)) -> None:
        if x_gateway_internal_key != app_settings.internal_key:
            raise HTTPException(status_code=401, detail="invalid gateway internal key")

    async def require_register_key(x_gateway_register_key: str | None = Header(default=None)) -> None:
        if x_gateway_register_key != app_settings.register_key:
            raise HTTPException(status_code=401, detail="invalid gateway register key")

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

    @router.get("/nodes", response_model=list[GatewayNodeResponse])
    async def list_nodes(_: None = Depends(require_internal_key)):
        nodes = await repo.list_nodes()
        return [_to_node_response(node, app_settings) for node in nodes]

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

    @router.get("/capabilities", response_model=CapabilitiesResponse)
    async def capabilities(_: None = Depends(require_internal_key)):
        return await gateway_service.get_capabilities()

    async def forward_to_node(node_id: str, method: str, path: str, body: Any | None):
        node = await repo.get_node(node_id)
        if node is None:
            raise HTTPException(status_code=404, detail="node_not_found")
        if not node.enabled:
            raise HTTPException(status_code=409, detail="node_disabled")
        try:
            response = await child_client.forward(node.base_url, node.internal_key, method, path, body)
        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail="node_timeout") from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="node_unreachable") from exc
        return Response(
            content=response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type") or "application/json",
        )

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

    app.include_router(router, prefix=app_settings.api_prefix)
    return app
