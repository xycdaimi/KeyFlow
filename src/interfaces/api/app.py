"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-13
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
from infrastructure.db.bootstrap import bootstrap_write_database
from infrastructure.db.repository_impl import SqlAlchemyKeyRepository
from infrastructure.db.sqlite_bootstrap import bootstrap_sqlite_database
from interfaces.api.routes.admin import router as admin_router
from interfaces.api.routes.allocate import router as allocate_router
from interfaces.api.routes.health import router as health_router
from interfaces.api.routes.report import router as report_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        repository: SqlAlchemyKeyRepository = app.state.container.resolve(SqlAlchemyKeyRepository)
    except punq.MissingDependencyError as exc:
        logger.info(
            "event=lifespan_runtime_dependencies_missing source=api_startup error=%s",
            exc,
        )
        yield
        return

    write_engine = repository._write_factory.kw["bind"]
    read_engine = repository._read_factory.kw["bind"]
    redis_cache: RedisKeyCache | None = None

    if getattr(app.state.settings, "runtime_mode", "dev") == "local":
        await bootstrap_sqlite_database(
            app.state.settings.local_sqlite_path,
            write_engine,
        )
    else:
        redis_cache = app.state.container.resolve(RedisKeyCache)
        await bootstrap_write_database(
            app.state.settings.database_write_url,
            write_engine,
        )

    try:
        yield
    finally:
        if redis_cache is not None:
            await redis_cache._redis.aclose()
        await write_engine.dispose()
        if read_engine is not write_engine:
            await read_engine.dispose()


def attach_health_checkers(app: FastAPI, container: punq.Container) -> None:
    """Register async check callables on app.state when DB and Redis are available in the container."""
    try:
        repository: SqlAlchemyKeyRepository = container.resolve(SqlAlchemyKeyRepository)
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
            return False, "检查数据库失败"#str(exc)

    async def check_redis() -> tuple[bool, str | None]:
        try:
            await redis_cache._redis.ping()
            return True, None
        except Exception as exc:
            return False, str(exc)

    settings = container.resolve(Settings)
    if settings.runtime_mode == "local":
        app.state.health_checkers = {
            "app": check_app,
            "database": check_database,
        }
        return

    redis_cache: RedisKeyCache = container.resolve(RedisKeyCache)
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
