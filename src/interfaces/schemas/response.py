"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-06-05
@Description: API 响应数据模型
"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from domain.value_objects.key_pool import KeyPool
from domain.value_objects.key_status import KeyStatus


class ProviderInfoResponse(BaseModel):
    name: str
    description: str
    auth_type: str
    credential_hint: str
    model_source: str
    available: bool


class KeyResponse(BaseModel):
    id: str
    provider: str
    status: KeyStatus
    supported_models: list[str]
    quota_used: int
    last_used_at: datetime | None
    cooldown_until: datetime | None
    max_concurrent_uses: int


class AllocateResponse(BaseModel):
    key_id: str
    lease_id: str
    provider_model: str | None
    credential: dict[str, Any]


class AllocateByModelResponse(BaseModel):
    key_id: str
    lease_id: str
    provider: str
    provider_model: str
    credential: dict[str, Any]


class OperationStatusResponse(BaseModel):
    status: str


class CreateKeyResponse(BaseModel):
    status: str
    key_id: str


class AdminKeyListItemResponse(BaseModel):
    key_id: str
    credential: dict[str, Any]
    pool: KeyPool
    max_concurrent_uses: int
    status: KeyStatus


class AdminKeyDetailResponse(BaseModel):
    credential: dict[str, Any]
    pool: KeyPool
    max_concurrent_uses: int
    status: KeyStatus


class KeyModelsResponse(BaseModel):
    models: list[str]
