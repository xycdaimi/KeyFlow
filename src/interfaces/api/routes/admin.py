"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-29
@Description: 管理端 API（Key / Provider 维护）
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status

from application.services.key_service import CreateKeyInput, KeyService, UpdateKeyInput
from domain.entities.api_key import ApiKey
from domain.exceptions.domain_exceptions import (
    DuplicateCredentialError,
    InvalidCredentialError,
    KeyNotFoundError,
    ProviderNotFoundError,
    ProviderNotReadyError,
    RuntimeLockUnavailableError,
    UpstreamUnreachableError,
)
from infrastructure.config.settings import Settings
from infrastructure.plugins.base import ProviderRegistry
from interfaces.api.deps import get_key_service, get_provider_registry, get_settings
from interfaces.middleware.auth import verify_internal_key
from interfaces.schemas.request import CreateKeyRequest, MoveKeyPoolRequest, UpdateKeyRequest
from interfaces.schemas.response import (
    AdminKeyDetailResponse,
    AdminKeyListItemResponse,
    CreateKeyResponse,
    KeyModelsResponse,
    OperationStatusResponse,
    ProviderInfoResponse,
)


async def require_admin_internal_key(
    x_internal_key: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    await verify_internal_key(settings, x_internal_key)


router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin_internal_key)])


def _to_list_item_response(key: ApiKey) -> AdminKeyListItemResponse:
    return AdminKeyListItemResponse(
        key_id=key.id,
        credential=key.credential,
        pool=key.pool,
        max_concurrent_uses=key.max_concurrent_uses,
        status=key.status,
    )


def _to_detail_response(key: ApiKey) -> AdminKeyDetailResponse:
    return AdminKeyDetailResponse(
        credential=key.credential,
        pool=key.pool,
        max_concurrent_uses=key.max_concurrent_uses,
        status=key.status,
    )


@router.post("/providers/{provider}/keys", response_model=CreateKeyResponse)
async def create_key(
    provider: str,
    payload: CreateKeyRequest,
    service: KeyService = Depends(get_key_service),
) -> CreateKeyResponse:
    try:
        key = await service.create_key(
            CreateKeyInput(
                provider=provider,
                credential=payload.credential,
                pool=payload.pool,
                max_concurrent_uses=payload.max_concurrent_uses,
            )
        )
    except DuplicateCredentialError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="duplicate_credential") from exc
    except ProviderNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider_not_found") from exc
    except ProviderNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="provider_not_ready") from exc
    except UpstreamUnreachableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="upstream_unreachable",
        ) from exc
    except InvalidCredentialError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return CreateKeyResponse(status="ok", key_id=key.id)


@router.get("/providers/{provider}/keys", response_model=list[AdminKeyListItemResponse])
async def list_provider_keys(
    provider: str,
    service: KeyService = Depends(get_key_service),
) -> list[AdminKeyListItemResponse]:
    keys = await service.list_keys(provider)
    return [_to_list_item_response(k) for k in keys]


@router.get("/providers/{provider}/keys/{key_id}/models", response_model=KeyModelsResponse)
async def get_key_models(
    provider: str,
    key_id: str,
    service: KeyService = Depends(get_key_service),
) -> KeyModelsResponse:
    try:
        models = await service.get_key_models(provider, key_id)
    except KeyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key_not_found") from exc
    return KeyModelsResponse(models=models)


@router.get("/keys/{key_id}", response_model=AdminKeyDetailResponse)
async def get_key(
    key_id: str,
    service: KeyService = Depends(get_key_service),
) -> AdminKeyDetailResponse:
    try:
        key = await service.get_key(key_id)
    except KeyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key_not_found") from exc
    return _to_detail_response(key)


@router.put("/keys/{key_id}", response_model=OperationStatusResponse)
async def update_key(
    key_id: str,
    payload: UpdateKeyRequest,
    service: KeyService = Depends(get_key_service),
) -> OperationStatusResponse:
    try:
        await service.update_key(
            key_id,
            UpdateKeyInput(
                credential=payload.credential,
                status=payload.status,
                max_concurrent_uses=payload.max_concurrent_uses,
            ),
        )
    except DuplicateCredentialError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="duplicate_credential") from exc
    except KeyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key_not_found") from exc
    except ProviderNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="provider_not_found") from exc
    except ProviderNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="provider_not_ready") from exc
    except RuntimeLockUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="key_runtime_locked") from exc
    except UpstreamUnreachableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="upstream_unreachable",
        ) from exc
    except InvalidCredentialError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return OperationStatusResponse(status="ok")


@router.put("/keys/{key_id}/pool", response_model=OperationStatusResponse)
async def move_key_pool(
    key_id: str,
    payload: MoveKeyPoolRequest,
    service: KeyService = Depends(get_key_service),
) -> OperationStatusResponse:
    try:
        await service.move_key_pool(key_id, payload.pool)
    except KeyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key_not_found") from exc
    return OperationStatusResponse(status="ok")


@router.delete("/keys/{key_id}", response_model=OperationStatusResponse)
async def delete_key(
    key_id: str,
    service: KeyService = Depends(get_key_service),
) -> OperationStatusResponse:
    try:
        await service.delete_key(key_id)
    except KeyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="key_not_found") from exc
    return OperationStatusResponse(status="ok")


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


@router.get("/providers", response_model=list[ProviderInfoResponse])
async def list_providers(
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> list[ProviderInfoResponse]:
    """列出系统中所有已注册的供应商插件及其认证信息。

    - name: 供应商标识符，调用其他接口时用作 {provider} 路径参数
    - description: 供应商说明
    - auth_type: 认证方式
    - credential_hint: 凭证格式说明
    - model_source: 供应商实现来源（remote=外部 API / 服务，static=本地 SDK / 本地代码实现）
    - available: 插件运行时依赖是否已满足
    """
    return [
        ProviderInfoResponse(
            name=plugin.name,
            description=plugin.description,
            auth_type=plugin.auth_type,
            credential_hint=plugin.credential_hint,
            model_source=plugin.model_source,
            available=plugin.is_plugin_ready(),
        )
        for plugin in registry.all()
    ]
