from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ApiKeyModel(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    api_key: Mapped[str] = mapped_column(String(512))
    """The actual credential. api_key IS the account."""

    status: Mapped[str] = mapped_column(String(32), default="available")
    quota_used: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    supported_models: Mapped[list] = mapped_column(JSON, default=list)
    """Model IDs fetched at registration. Informational only."""
