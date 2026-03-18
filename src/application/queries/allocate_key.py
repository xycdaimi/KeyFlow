from domain.entities.api_key import ApiKey

from application.services.key_service import KeyService


class AllocateKeyQuery:
    def __init__(self, service: KeyService) -> None:
        self._service = service

    async def execute(self, provider: str, model: str | None = None) -> ApiKey:
        return await self._service.allocate_key(provider, model)
