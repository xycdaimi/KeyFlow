from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = Field(default="KeyFlow", alias="APP_NAME")
    api_prefix: str = Field(default="/api", alias="API_PREFIX")
    internal_api_key: str = Field(default="dev-internal-key", alias="INTERNAL_API_KEY")

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
    weight_quota: float = Field(default=0.4, alias="WEIGHT_QUOTA")
    weight_idle: float = Field(default=0.25, alias="WEIGHT_IDLE")
    weight_success: float = Field(default=0.2, alias="WEIGHT_SUCCESS")
    weight_error: float = Field(default=0.1, alias="WEIGHT_ERROR")
    weight_rate_limit: float = Field(default=0.03, alias="WEIGHT_RATE_LIMIT")
    weight_cooldown: float = Field(default=0.02, alias="WEIGHT_COOLDOWN")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
