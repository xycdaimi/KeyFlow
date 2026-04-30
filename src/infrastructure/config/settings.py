"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-16
@Description: 应用运行时配置
"""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="KeyFlow", alias="APP_NAME")
    app_description: str = Field(
        default="Provider-scoped API key scheduling service.",
        alias="APP_DESCRIPTION",
    )
    app_version: str = Field(default="0.1.0", alias="APP_VERSION")
    api_prefix: str = Field(default="/api", alias="API_PREFIX")
    internal_api_key: str = Field(default="dev-internal-key", alias="INTERNAL_API_KEY")
    model_alias_config_path: str | None = Field(default=None, alias="MODEL_ALIAS_CONFIG_PATH")

    database_read_url: str = Field(
        default="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        alias="DATABASE_URL_READ",
    )
    database_write_url: str = Field(
        default="postgresql+asyncpg://keyflow:keyflow@localhost:5432/keyflow",
        alias="DATABASE_URL_WRITE",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    allocate_idle_cap_seconds: int = Field(default=300, alias="ALLOCATE_IDLE_CAP_SECONDS")
    allocate_error_cap: int = Field(default=10, alias="ALLOCATE_ERROR_CAP")
    allocate_jitter: float = Field(default=0.01, alias="ALLOCATE_JITTER")
    allocate_lease_seconds: int = Field(default=2, alias="ALLOCATE_LEASE_SECONDS")
    weight_quota: float = Field(default=0.4, alias="WEIGHT_QUOTA")
    capacity_unknown_fallback: float = Field(default=0.5, alias="CAPACITY_UNKNOWN_FALLBACK")
    weight_idle: float = Field(default=0.25, alias="WEIGHT_IDLE")
    weight_success: float = Field(default=0.2, alias="WEIGHT_SUCCESS")
    weight_error: float = Field(default=0.1, alias="WEIGHT_ERROR")
    weight_rate_limit: float = Field(default=0.03, alias="WEIGHT_RATE_LIMIT")
    weight_cooldown: float = Field(default=0.02, alias="WEIGHT_COOLDOWN")
    refresh_cache_seconds: int = Field(default=60, alias="REFRESH_CACHE_SECONDS")
    """Cache validity. Keys with last_refreshed_at within this window use cached availability."""
    background_task_interval_seconds: int = Field(default=60, alias="BACKGROUND_TASK_INTERVAL_SECONDS")
    """Interval for recover_cooldowns and refresh_keys background task."""
    global_http_proxy: str | None = Field(default=None, alias="GLOBAL_HTTP_PROXY")
    """Fixed HTTP proxy used by provider plugins whose egress mode is proxy."""
    http_connect_timeout: float = Field(default=3.0, alias="HTTP_CONNECT_TIMEOUT")
    http_read_timeout: float = Field(default=8.0, alias="HTTP_READ_TIMEOUT")
    http_total_timeout: float = Field(default=12.0, alias="HTTP_TOTAL_TIMEOUT")
    """Default HTTP timeout settings for provider plugin upstream calls."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
