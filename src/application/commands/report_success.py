from domain.entities.api_key import ApiKey

from application.services.key_service import KeyService


class ReportSuccessCommand:
    def __init__(self, service: KeyService) -> None:
        self._service = service

    async def execute(self, key_id: str, tokens_used: int = 0) -> ApiKey:
        return await self._service.report_success(key_id, tokens_used=tokens_used)
