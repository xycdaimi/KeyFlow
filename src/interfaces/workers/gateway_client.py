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
