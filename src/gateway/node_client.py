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

    async def forward(self, base_url: str, internal_key: str, method: str, path: str, json_body: Any | None) -> httpx.Response:
        async with httpx.AsyncClient(base_url=base_url, timeout=self._timeout, transport=self._transport) as client:
            return await client.request(method, path, headers={"X-Internal-Key": internal_key}, json=json_body)
