from domain.entities.api_key import ApiKey

from application.services.key_service import CreateKeyInput, KeyService


class CreateKeyCommand:
    def __init__(self, service: KeyService) -> None:
        self._service = service

    async def execute(self, data: CreateKeyInput) -> ApiKey:
        return await self._service.create_key(data)
