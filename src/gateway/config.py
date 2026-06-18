"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-19
@Description: KeyFlow gateway 控制面配置
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env.gateway", env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="KeyFlow Gateway", alias="APP_NAME")
    app_version: str = Field(default="0.1.0", alias="APP_VERSION")
    api_prefix: str = Field(default="/api/gateway", alias="API_PREFIX")
    runtime_mode: str = Field(default="local", alias="KEYFLOW_RUNTIME_MODE")
    sqlite_path: str = Field(default="data/keyflow_gateway.db", alias="LOCAL_SQLITE_PATH")
    internal_key: str = Field(default="dev-gateway-internal-key", alias="GATEWAY_INTERNAL_KEY")
    register_key: str = Field(default="dev-gateway-register-key", alias="GATEWAY_REGISTER_KEY")
    heartbeat_timeout_seconds: int = Field(default=90, alias="GATEWAY_HEARTBEAT_TIMEOUT_SECONDS")
    stale_node_retention_seconds: int = Field(default=86400, alias="GATEWAY_STALE_NODE_RETENTION_SECONDS")
    node_http_connect_timeout_seconds: float = Field(default=1.0, alias="GATEWAY_NODE_HTTP_CONNECT_TIMEOUT_SECONDS")
    node_http_read_timeout_seconds: float = Field(default=5.0, alias="GATEWAY_NODE_HTTP_READ_TIMEOUT_SECONDS")
    node_probe_cache_seconds: int = Field(default=15, alias="GATEWAY_NODE_PROBE_CACHE_SECONDS")


@lru_cache(maxsize=1)
def get_gateway_settings() -> GatewaySettings:
    return GatewaySettings()
