"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-27
@Description: 应用依赖注入容器
"""
from __future__ import annotations

import punq

from application.services.key_service import KeyService
from application.services.model_alias_resolver import ModelAliasResolver
from domain.services.scheduler import KeyScheduler
from domain.services.scorer import KeyScorer, ScoreWeights
from domain.services.state_machine import KeyStateMachine
from infrastructure.cache.key_cache import RedisKeyCache
from infrastructure.cache.redis_client import create_redis_client
from infrastructure.config.settings import Settings
from infrastructure.db.repository_impl import SqlAlchemyKeyRepository
from infrastructure.db.session import create_session_factory
from infrastructure.logging.logger import configure_logging
from infrastructure.plugins.base import ProviderRegistry
from infrastructure.plugins.providers import *


def create_container(settings: Settings) -> punq.Container:
    configure_logging(settings.log_level)

    read_factory, write_factory = create_session_factory(
        settings.database_read_url,
        settings.database_write_url,
    )
    redis = create_redis_client(settings.redis_url)

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
    state_machine = KeyStateMachine()
    repository = SqlAlchemyKeyRepository(read_factory, write_factory)
    allocation_store = RedisKeyCache(redis)
    provider_registry = ProviderRegistry()
    provider_registry.register(OpenAIPlugin())
    provider_registry.register(AnthropicPlugin())
    provider_registry.register(AntigravityOpenAiPlugin())
    provider_registry.register(AntigravityOauthPlugin())
    provider_registry.register(GeminiPlugin())
    provider_registry.register(GeminiOauthPlugin())
    provider_registry.register(GeminiOpenAiPlugin())
    provider_registry.register(OpenRouterPlugin())
    provider_registry.register(GeminiWebProxyPlugin())
    provider_registry.register(CodexOpenAiPlugin())
    provider_registry.register(CodexOauthPlugin())
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
    container.register(RedisKeyCache, instance=allocation_store)
    container.register(KeyService, instance=service)
    return container
