from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging

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

logger = logging.getLogger(__name__)
DB_SCHEMA_INIT_MAX_ATTEMPTS = 5
DB_SCHEMA_INIT_RETRY_SECONDS = 2


async def ensure_schema_ready(write_engine) -> None:
    for attempt in range(1, DB_SCHEMA_INIT_MAX_ATTEMPTS + 1):
        try:
            async with write_engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            logger.info("event=db_schema_ready source=api_startup attempt=%s", attempt)
            return
        except Exception as exc:
            if attempt >= DB_SCHEMA_INIT_MAX_ATTEMPTS:
                logger.exception(
                    "event=db_schema_init_failed source=api_startup attempts=%s error=%s",
                    attempt,
                    exc,
                )
                raise
            logger.warning(
                "event=db_schema_init_retry source=api_startup attempt=%s max_attempts=%s retry_in_seconds=%s error=%s",
                attempt,
                DB_SCHEMA_INIT_MAX_ATTEMPTS,
                DB_SCHEMA_INIT_RETRY_SECONDS,
                exc,
            )
            await asyncio.sleep(DB_SCHEMA_INIT_RETRY_SECONDS)


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

    # Startup schema guard:
    # create_all() only creates missing tables; existing tables are left intact.
    await ensure_schema_ready(write_engine)

    try:
        yield
    finally:
        await redis_cache._redis.aclose()
        await write_engine.dispose()
        await read_engine.dispose()


def create_app(container: punq.Container | None = None, settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    app_container = container or create_container(app_settings)

    app = FastAPI(
        title=app_settings.app_name,
        description=app_settings.app_description,
        version=app_settings.app_version,
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.container = app_container

    app.include_router(health_router)
    app.include_router(allocate_router, prefix=app_settings.api_prefix)
    app.include_router(report_router, prefix=app_settings.api_prefix)
    app.include_router(admin_router, prefix=app_settings.api_prefix)
    return app
