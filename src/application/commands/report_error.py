from domain.entities.api_key import ApiKey

from application.services.key_service import KeyService


class ReportErrorCommand:
    def __init__(self, service: KeyService) -> None:
        self._service = service

    async def execute(self, key_id: str, error_type: str) -> ApiKey:
        return await self._service.report_error(key_id, error_type)
