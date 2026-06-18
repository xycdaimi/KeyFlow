"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-06-08
@Description: 应用依赖注入容器
"""
from __future__ import annotations

import punq

from application.services.key_service import KeyService
from application.services.model_alias_resolver import ModelAliasResolver
from domain.services.scheduler import KeyScheduler
from domain.services.scorer import KeyScorer, ScoreWeights
from domain.services.state_machine import KeyStateMachine
from infrastructure.cache.composite_key_cache import CompositeKeyCache
from infrastructure.cache.db_key_cache import DatabaseKeyCache
from infrastructure.cache.key_cache import RedisKeyCache
from infrastructure.cache.redis_client import create_redis_client
from infrastructure.cache.sqlite_key_cache import SqliteKeyCache
from infrastructure.config.settings import Settings
from infrastructure.db.repository_impl import SqlAlchemyKeyRepository
from infrastructure.db.session import create_session_factory
from infrastructure.db.sqlite_session import create_sqlite_session_factory
from infrastructure.logging.logger import configure_logging
from infrastructure.plugins.base import ProviderRegistry
from infrastructure.plugins.providers import *


def _parse_report_backoff_minutes(value: str) -> tuple[int, ...]:
    result: list[int] = []
    for item in value.split(","):
        stripped = item.strip()
        if not stripped:
            continue
        result.append(max(int(stripped), 1))
    return tuple(result) or (1,)


def create_container(settings: Settings) -> punq.Container:
    configure_logging(settings.log_level)

    if settings.runtime_mode == "local":
        read_factory, write_factory = create_sqlite_session_factory(settings.local_sqlite_path)
        repository = SqlAlchemyKeyRepository(read_factory, write_factory)
        allocation_store = SqliteKeyCache(write_factory.kw["bind"])
    elif settings.runtime_mode == "dev":
        read_factory, write_factory = create_session_factory(
            settings.database_read_url,
            settings.database_write_url,
        )
        redis = create_redis_client(settings.redis_url)
        repository = SqlAlchemyKeyRepository(read_factory, write_factory)
        redis_store = RedisKeyCache(redis)
        database_store = DatabaseKeyCache(write_factory)
        allocation_store = CompositeKeyCache(redis_store, database_store)
    else:
        raise ValueError("KEYFLOW_RUNTIME_MODE must be one of: dev, local")

    scorer = KeyScorer(
        ScoreWeights(
            capacity=settings.weight_quota,
            idle=settings.weight_idle,
            success=settings.weight_success,
            error=settings.weight_error,
            rate_limit=settings.weight_rate_limit,
            cooldown=settings.weight_cooldown,
            capacity_unknown_fallback=settings.capacity_unknown_fallback,
            idle_cap_seconds=settings.allocate_idle_cap_seconds,
            error_cap=settings.allocate_error_cap,
        )
    )
    scheduler = KeyScheduler(scorer, jitter=settings.allocate_jitter)
    state_machine = KeyStateMachine(
        report_transient_failure_threshold=settings.report_transient_failure_threshold,
        report_cooldown_disable_rounds=settings.report_cooldown_disable_rounds,
        report_backoff_minutes=_parse_report_backoff_minutes(settings.report_backoff_minutes),
    )
    provider_registry = ProviderRegistry()
    provider_registry.register(OpenAIPlugin())
    provider_registry.register(AnthropicPlugin())
    provider_registry.register(AntigravityOpenAiPlugin())
    provider_registry.register(AntigravityOauthPlugin())
    provider_registry.register(GeminiPlugin())
    provider_registry.register(GeminiCustomPlugin())
    provider_registry.register(GeminiOauthPlugin())
    provider_registry.register(GeminiOpenAiPlugin())
    provider_registry.register(OpenRouterPlugin())
    provider_registry.register(GeminiWebProxyPlugin())
    provider_registry.register(CodexOpenAiPlugin())
    provider_registry.register(CodexOauthPlugin())
    provider_registry.register(QwenImageEditPlugin())
    model_alias_resolver = ModelAliasResolver.from_yaml_file(settings.model_alias_config_path)
    service = KeyService(
        repository,
        allocation_store,
        scheduler,
        scorer,
        state_machine,
        provider_registry,
        model_alias_resolver=model_alias_resolver,
        allocation_lease_seconds=settings.allocate_lease_seconds,
        refresh_cache_seconds=settings.refresh_cache_seconds,
    )

    container = punq.Container()
    container.register(Settings, instance=settings)
    container.register(KeyScorer, instance=scorer)
    container.register(KeyScheduler, instance=scheduler)
    container.register(KeyStateMachine, instance=state_machine)
    container.register(ProviderRegistry, instance=provider_registry)
    container.register(ModelAliasResolver, instance=model_alias_resolver)
    container.register(SqlAlchemyKeyRepository, instance=repository)
    if isinstance(allocation_store, CompositeKeyCache):
        container.register(CompositeKeyCache, instance=allocation_store)
        container.register(RedisKeyCache, instance=redis_store)
        container.register(DatabaseKeyCache, instance=database_store)
    if isinstance(allocation_store, SqliteKeyCache):
        container.register(SqliteKeyCache, instance=allocation_store)
    container.register(KeyService, instance=service)
    return container
