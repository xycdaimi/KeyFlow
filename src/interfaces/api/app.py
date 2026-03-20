"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-03-20
@Description: FastAPI 应用工厂与生命周期资源管理
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging

import punq
from fastapi import FastAPI

from sqlalchemy import text

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


async def ensure_refresh_columns(conn) -> None:
    """Add last_refreshed_at, cached_available, cached_capacity_score if missing."""
    for col, sql_type in [
        ("last_refreshed_at", "TIMESTAMP WITH TIME ZONE"),
        ("cached_available", "BOOLEAN"),
        ("cached_capacity_score", "DOUBLE PRECISION"),
    ]:
        await conn.execute(
            text(f"ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS {col} {sql_type}")
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        repository: SqlAlchemyKeyRepository = app.state.container.resolve(SqlAlchemyKeyRepository)
        redis_cache: RedisKeyCache = app.state.container.resolve(RedisKeyCache)
    except punq.MissingDependencyError as exc:
        logger.info(
            "event=lifespan_runtime_dependencies_missing source=api_startup error=%s",
            exc,
        )
        yield
        return

    write_engine = repository._write_factory.kw["bind"]
    read_engine = repository._read_factory.kw["bind"]

    # Startup schema guard:
    # create_all() only creates missing tables; existing tables are left intact.
    await ensure_schema_ready(write_engine)

    async with write_engine.begin() as conn:
        await ensure_refresh_columns(conn)

    try:
        yield
    finally:
        await redis_cache._redis.aclose()
        await write_engine.dispose()
        await read_engine.dispose()


def attach_health_checkers(app: FastAPI, container: punq.Container) -> None:
    """Register async check callables on app.state when DB and Redis are available in the container."""
    try:
        repository: SqlAlchemyKeyRepository = container.resolve(SqlAlchemyKeyRepository)
        redis_cache: RedisKeyCache = container.resolve(RedisKeyCache)
    except punq.MissingDependencyError:
        return

    async def check_app() -> tuple[bool, str | None]:
        return True, None

    async def check_database() -> tuple[bool, str | None]:
        try:
            async with repository._read_factory() as session:
                await session.execute(text("SELECT 1"))
            return True, None
        except Exception as exc:
            return False, str(exc)

    async def check_redis() -> tuple[bool, str | None]:
        try:
            await redis_cache._redis.ping()
            return True, None
        except Exception as exc:
            return False, str(exc)

    app.state.health_checkers = {
        "app": check_app,
        "database": check_database,
        "redis": check_redis,
    }


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
    attach_health_checkers(app, app_container)

    app.include_router(health_router)
    app.include_router(allocate_router, prefix=app_settings.api_prefix)
    app.include_router(report_router, prefix=app_settings.api_prefix)
    app.include_router(admin_router, prefix=app_settings.api_prefix)
    return app
