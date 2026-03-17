from fastapi import Request

from application.services.key_service import KeyService
from infrastructure.config.settings import Settings


def get_container(request: Request):
    return request.app.state.container


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_key_service(request: Request) -> KeyService:
    return request.app.state.container.resolve(KeyService)
