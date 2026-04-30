"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-27
@Description: SQLAlchemy 数据库模型
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ApiKeyModel(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        Index(
            "uq_api_keys_provider_credential",
            "provider",
            text("(md5((credential::jsonb)::text))"),
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    credential: Mapped[dict] = mapped_column(JSON)
    """Provider-defined structured credential payload."""

    status: Mapped[str] = mapped_column(String(32), default="available")
    quota_used: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    supported_models: Mapped[list] = mapped_column(JSON, default=list)
    """Model IDs fetched at registration. Informational only."""
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Last time availability/capacity was refreshed. Used to avoid duplicate refresh across processes."""
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Last time this row was modified."""
    cached_available: Mapped[bool | None] = mapped_column(nullable=True)
    """Cached credential-level availability result. None = not yet refreshed."""
    cached_quota_available: Mapped[bool | None] = mapped_column(nullable=True)
    """Cached quota availability result. None = unknown."""
    cached_capacity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Cached capacity score from plugin. None = unknown."""
    runtime_lock_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """Owner token for runtime snapshot refresh lock."""
    runtime_lock_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Expiration time for runtime snapshot refresh lock."""
    runtime_lock_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """Human-readable runtime lock reason for debugging."""
