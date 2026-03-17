from datetime import datetime

from pydantic import BaseModel

from domain.value_objects.key_status import KeyStatus


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
    api_key: str


class GenericStatusResponse(BaseModel):
    status: str
    key: KeyResponse
