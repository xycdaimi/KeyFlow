from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ApiKeyModel(Base):
    __tablename__ = "api_keys"

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
    disabled_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    supported_models: Mapped[list] = mapped_column(JSON, default=list)
    """Model IDs fetched at registration. Informational only."""
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Last time availability/capacity was refreshed. Used to avoid duplicate refresh across processes."""
    cached_available: Mapped[bool | None] = mapped_column(nullable=True)
    """Cached is_credential_available result. None = not yet refreshed."""
    cached_capacity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Cached capacity score from plugin. None = unknown."""
