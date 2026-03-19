from datetime import datetime

from pydantic import BaseModel

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


class AllocateResponse(BaseModel):
    status: str
    key_id: str
    credential: dict[str, str]


class OperationStatusResponse(BaseModel):
    status: str


class AdminKeyListItemResponse(BaseModel):
    key_id: str
    credential: dict[str, str]
    status: KeyStatus


class AdminKeyDetailResponse(BaseModel):
    credential: dict[str, str]
    status: KeyStatus


class KeyModelsResponse(BaseModel):
    models: list[str]


class GenericStatusResponse(BaseModel):
    status: str
    key: KeyResponse
