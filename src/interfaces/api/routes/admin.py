from fastapi import APIRouter, Depends, HTTPException, status

from application.services.key_service import CreateKeyInput, KeyService, UpdateKeyInput
from domain.entities.api_key import ApiKey
from domain.exceptions.domain_exceptions import KeyNotFoundError
from interfaces.api.deps import get_key_service
from interfaces.schemas.request import CreateKeyRequest, UpdateKeyRequest
from interfaces.schemas.response import KeyResponse

router = APIRouter(tags=["admin"])


def _to_response(key: ApiKey) -> KeyResponse:
    return KeyResponse(
        id=key.id,
        provider=key.provider,
        status=key.status,
        supported_models=key.supported_models,
        quota_used=key.quota_used,
        last_used_at=key.last_used_at,
        cooldown_until=key.cooldown_until,
    )


@router.post("/providers/{provider}/keys", response_model=KeyResponse)
async def create_key(
    provider: str,
    payload: CreateKeyRequest,
    service: KeyService = Depends(get_key_service),
) -> KeyResponse:
    key = await service.create_key(CreateKeyInput(provider=provider, api_key=payload.api_key))
    return _to_response(key)


@router.get("/providers/{provider}/keys", response_model=list[KeyResponse])
async def list_provider_keys(
    provider: str,
    service: KeyService = Depends(get_key_service),
) -> list[KeyResponse]:
    keys = await service.list_keys(provider)
    return [_to_response(k) for k in keys]


@router.put("/keys/{key_id}", response_model=KeyResponse)
async def update_key(
    key_id: str,
    payload: UpdateKeyRequest,
    service: KeyService = Depends(get_key_service),
) -> KeyResponse:
    try:
        key = await service.update_key(key_id, UpdateKeyInput(api_key=payload.api_key, status=payload.status))
    except KeyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key_not_found") from exc
    return _to_response(key)


@router.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_key(
    key_id: str,
    service: KeyService = Depends(get_key_service),
) -> None:
    try:
        await service.delete_key(key_id)
    except KeyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key_not_found") from exc


@router.get("/keys/{key_id}/explain")
async def explain_key(
    key_id: str,
    service: KeyService = Depends(get_key_service),
) -> dict:
    """Return plugin-provided credential summary for admin display.

    The response is plugin-defined. It must NOT contain the raw credential.
    """
    try:
        return await service.get_key_explain(key_id)
    except KeyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key_not_found") from exc
