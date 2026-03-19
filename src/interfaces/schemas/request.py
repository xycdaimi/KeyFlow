from pydantic import BaseModel, Field

from domain.value_objects.key_status import KeyStatus


class AllocateRequest(BaseModel):
    provider: str
    model: str | None = None


class ReportErrorRequest(BaseModel):
    key_id: str
    error_type: str


class ReportSuccessRequest(BaseModel):
    key_id: str
    tokens_used: int = Field(default=0, ge=0)


class CreateKeyRequest(BaseModel):
    """Add a new credential account to the pool.

    The provider-specific credential fields are stored in a structured dict.
    Other info (balance, pricing, models) is fetched from the provider plugin.
    """

    credential: dict[str, str]


class UpdateKeyRequest(BaseModel):
    """Update an existing account.

    Only the credential payload or the admin-controlled status can be changed.
    Balance / pricing / quota data is managed by the plugin sync, not user input.
    """

    credential: dict[str, str] | None = None
    status: KeyStatus | None = None
