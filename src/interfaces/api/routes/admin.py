from fastapi import APIRouter, Depends, HTTPException, status

from application.services.key_service import CreateKeyInput, KeyService, UpdateKeyInput
from domain.entities.api_key import ApiKey
from domain.exceptions.domain_exceptions import DuplicateCredentialError, KeyNotFoundError
from infrastructure.plugins.base import ProviderRegistry
from interfaces.api.deps import get_key_service, get_provider_registry
from interfaces.schemas.request import CreateKeyRequest, UpdateKeyRequest
from interfaces.schemas.response import (
    AdminKeyDetailResponse,
    AdminKeyListItemResponse,
    CreateKeyResponse,
    KeyModelsResponse,
    OperationStatusResponse,
    ProviderInfoResponse,
)

router = APIRouter(tags=["admin"])


def _to_list_item_response(key: ApiKey) -> AdminKeyListItemResponse:
    return AdminKeyListItemResponse(
        key_id=key.id,
        credential=key.credential,
        status=key.status,
    )


def _to_detail_response(key: ApiKey) -> AdminKeyDetailResponse:
    return AdminKeyDetailResponse(
        credential=key.credential,
        status=key.status,
    )


@router.post("/providers/{provider}/keys", response_model=CreateKeyResponse)
async def create_key(
    provider: str,
    payload: CreateKeyRequest,
    service: KeyService = Depends(get_key_service),
) -> CreateKeyResponse:
    try:
        key = await service.create_key(CreateKeyInput(provider=provider, credential=payload.credential))
    except DuplicateCredentialError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="duplicate_credential") from exc
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
        await service.update_key(key_id, UpdateKeyInput(credential=payload.credential, status=payload.status))
    except DuplicateCredentialError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="duplicate_credential") from exc
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
    - description: 供应商说明（API 类型、计费方式、可用性判断依据等）
    - auth_type: 认证方式（bearer_api_key / header_api_key / cookie）
    - credential_hint: 凭据格式说明（添加 key 时填入 credential 字段的 JSON 结构）
    - model_source: 模型列表来源（remote=从供应商 API 拉取 / static=内置固定列表）
    - available: 插件运行时依赖是否已满足（False 表示缺少依赖，该供应商无法分配）
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
