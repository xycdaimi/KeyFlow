from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from application.services.key_service import KeyService
from domain.exceptions.domain_exceptions import NoAvailableKeyError
from infrastructure.config.settings import Settings
from interfaces.api.deps import get_key_service, get_settings
from interfaces.middleware.auth import verify_internal_key
from interfaces.schemas.request import AllocateRequest
from interfaces.schemas.response import AllocateResponse

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/allocate-key", response_model=AllocateResponse)
async def allocate_key(
    payload: AllocateRequest,
    x_internal_key: Annotated[str | None, Header()] = None,
    service: KeyService = Depends(get_key_service),
    settings: Settings = Depends(get_settings),
) -> AllocateResponse:
    await verify_internal_key(settings, x_internal_key)
    try:
        key = await service.allocate_key(payload.provider, payload.model)
    except NoAvailableKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no_available_key",
        ) from exc

    return AllocateResponse(
        status="ok",
        key_id=key.id,
        api_key=key.api_key,
    )
