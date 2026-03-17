from pydantic import BaseModel, Field

from domain.value_objects.key_status import KeyStatus


class AllocateRequest(BaseModel):
    provider: str
    model: str | None = None
    task_type: str | None = None


class ReportErrorRequest(BaseModel):
    key_id: str
    error_type: str


class ReportSuccessRequest(BaseModel):
    key_id: str
    tokens_used: int = Field(default=0, ge=0)


class CreateKeyRequest(BaseModel):
    """Add a new api-key account to the pool.

    api_key IS the account — no display_name, no quota_total.
    Other info (balance, pricing, models) is fetched from the provider plugin.
    """

    api_key: str


class UpdateKeyRequest(BaseModel):
    """Update an existing account.

    Only the credential itself or the admin-controlled status can be changed.
    Balance / pricing / quota data is managed by the plugin sync, not user input.
    """

    api_key: str | None = None
    status: KeyStatus | None = None
