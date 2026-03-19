from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.entities.api_key import ApiKey
from domain.repositories.key_repository import KeyRepository
from domain.value_objects.key_status import KeyStatus
from infrastructure.db.models import ApiKeyModel


class SqlAlchemyKeyRepository(KeyRepository):
    def __init__(
        self,
        read_session_factory: async_sessionmaker[AsyncSession],
        write_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._read_factory = read_session_factory
        self._write_factory = write_session_factory

    async def list_provider_keys(self, provider: str) -> list[ApiKey]:
        return await self.list_keys(provider)

    async def list_keys(self, provider: str | None = None) -> list[ApiKey]:
        async with self._read_factory() as session:
            stmt = select(ApiKeyModel).order_by(ApiKeyModel.provider, ApiKeyModel.id)
            if provider:
                stmt = stmt.where(ApiKeyModel.provider == provider)
            result = await session.execute(stmt)
            return [self._to_entity(row) for row in result.scalars().all()]

    async def get_key(self, key_id: str) -> ApiKey | None:
        async with self._read_factory() as session:
            model = await session.get(ApiKeyModel, key_id)
            return self._to_entity(model) if model else None

    async def upsert_key(self, key: ApiKey) -> ApiKey:
        async with self._write_factory() as session:
            model = await session.get(ApiKeyModel, key.id)
            if model is None:
                model = ApiKeyModel(id=key.id)
                session.add(model)

            model.provider = key.provider
            model.credential = key.credential
            model.status = key.status.value
            model.quota_used = key.quota_used
            model.success_count = key.success_count
            model.error_count = key.error_count
            model.last_used_at = key.last_used_at
            model.cooldown_until = key.cooldown_until
            model.disabled_reason = key.disabled_reason
            model.supported_models = key.supported_models

            await session.commit()
            await session.refresh(model)
            return self._to_entity(model)

    async def delete_key(self, key_id: str) -> None:
        async with self._write_factory() as session:
            model = await session.get(ApiKeyModel, key_id)
            if model is None:
                return
            await session.delete(model)
            await session.commit()

    async def list_recoverable_keys(self, now: datetime) -> list[ApiKey]:
        async with self._read_factory() as session:
            stmt = select(ApiKeyModel).where(
                ApiKeyModel.cooldown_until.is_not(None),
                ApiKeyModel.cooldown_until <= now,
            )
            result = await session.execute(stmt)
            return [self._to_entity(row) for row in result.scalars().all()]

    def _to_entity(self, model: ApiKeyModel) -> ApiKey:
        return ApiKey(
            id=model.id,
            provider=model.provider,
            credential=model.credential,
            status=KeyStatus(model.status),
            quota_used=model.quota_used,
            last_used_at=model.last_used_at,
            success_count=model.success_count,
            error_count=model.error_count,
            cooldown_until=model.cooldown_until,
            disabled_reason=model.disabled_reason,
            supported_models=model.supported_models or [],
        )
