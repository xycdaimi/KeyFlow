from fastapi import HTTPException, status

from infrastructure.config.settings import Settings


async def verify_internal_key(settings: Settings, x_internal_key: str | None) -> None:
    if x_internal_key != settings.internal_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid internal key",
        )
