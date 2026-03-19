from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from application.services.key_service import KeyService
from domain.entities.api_key import ApiKey
from domain.exceptions.domain_exceptions import KeyNotFoundError
from infrastructure.config.settings import Settings
from interfaces.api.deps import get_key_service, get_settings
from interfaces.middleware.auth import verify_internal_key
from interfaces.schemas.request import ReportErrorRequest, ReportSuccessRequest
from interfaces.schemas.response import KeyResponse

router = APIRouter(prefix="/internal", tags=["internal"])


def _key_response(key: ApiKey) -> KeyResponse:
    return KeyResponse(
        id=key.id,
        provider=key.provider,
        status=key.status,
        supported_models=key.supported_models,
        quota_used=key.quota_used,
        last_used_at=key.last_used_at,
        cooldown_until=key.cooldown_until,
    )


@router.post("/report-error", response_model=KeyResponse)
async def report_error(
    payload: ReportErrorRequest,
    x_internal_key: Annotated[str | None, Header()] = None,
    service: KeyService = Depends(get_key_service),
    settings: Settings = Depends(get_settings),
) -> KeyResponse:
    await verify_internal_key(settings, x_internal_key)
    try:
        key = await service.report_error(payload.key_id, payload.error_type)
    except KeyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key_not_found") from exc
    return _key_response(key)


@router.post("/report-success", response_model=KeyResponse)
async def report_success(
    payload: ReportSuccessRequest,
    x_internal_key: Annotated[str | None, Header()] = None,
    service: KeyService = Depends(get_key_service),
    settings: Settings = Depends(get_settings),
) -> KeyResponse:
    await verify_internal_key(settings, x_internal_key)
    try:
        key = await service.report_success(payload.key_id, payload.tokens_used)
    except KeyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key_not_found") from exc
    return _key_response(key)
