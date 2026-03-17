from __future__ import annotations

from contextlib import asynccontextmanager

import punq
from fastapi import FastAPI

from container.container import create_container
from infrastructure.cache.key_cache import RedisKeyCache
from infrastructure.config.settings import Settings, get_settings
from infrastructure.db.models import Base
from infrastructure.db.repository_impl import SqlAlchemyKeyRepository
from interfaces.api.routes.admin import router as admin_router
from interfaces.api.routes.allocate import router as allocate_router
from interfaces.api.routes.health import router as health_router
from interfaces.api.routes.report import router as report_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        repository: SqlAlchemyKeyRepository = app.state.container.resolve(SqlAlchemyKeyRepository)
        redis_cache: RedisKeyCache = app.state.container.resolve(RedisKeyCache)
    except Exception:
        yield
        return

    write_engine = repository._write_factory.kw["bind"]
    read_engine = repository._read_factory.kw["bind"]

    async with write_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    try:
        yield
    finally:
        await redis_cache._redis.aclose()
        await write_engine.dispose()
        await read_engine.dispose()


def create_app(container: punq.Container | None = None, settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    app_container = container or create_container(app_settings)

    app = FastAPI(title=app_settings.app_name, lifespan=lifespan)
    app.state.settings = app_settings
    app.state.container = app_container

    app.include_router(health_router)
    app.include_router(allocate_router, prefix=app_settings.api_prefix)
    app.include_router(report_router, prefix=app_settings.api_prefix)
    app.include_router(admin_router, prefix=app_settings.api_prefix)
    return app
