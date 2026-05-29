from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from application.services.key_service import KeyService
from domain.exceptions.domain_exceptions import AllocationStoreUnavailableError, NoAvailableKeyError
from infrastructure.config.settings import Settings
from interfaces.api.deps import get_key_service, get_settings
from interfaces.middleware.auth import verify_internal_key
from interfaces.schemas.request import AllocateByModelRequest, AllocateRequest
from interfaces.schemas.response import AllocateByModelResponse, AllocateResponse

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
        allocation = await service.allocate_key(payload.provider, payload.model, pool=payload.pool)
    except NoAvailableKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no_available_key",
        ) from exc
    except AllocationStoreUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="allocation_store_unavailable",
        ) from exc

    return AllocateResponse(
        key_id=allocation.key.id,
        provider_model=allocation.provider_model,
        credential=allocation.key.credential,
    )


@router.post("/allocate-by-model", response_model=AllocateByModelResponse)
async def allocate_key_by_model(
    payload: AllocateByModelRequest,
    x_internal_key: Annotated[str | None, Header()] = None,
    service: KeyService = Depends(get_key_service),
    settings: Settings = Depends(get_settings),
) -> AllocateByModelResponse:
    await verify_internal_key(settings, x_internal_key)
    try:
        allocation = await service.allocate_key_by_model(payload.model, pool=payload.pool)
    except NoAvailableKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no_available_key",
        ) from exc
    except AllocationStoreUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="allocation_store_unavailable",
        ) from exc

    return AllocateByModelResponse(
        key_id=allocation.key.id,
        provider=allocation.key.provider,
        provider_model=allocation.provider_model or payload.model,
        credential=allocation.key.credential,
    )
