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
