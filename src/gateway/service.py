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
