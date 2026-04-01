from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select, update
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

    def _credential_equals(self, a: dict, b: dict) -> bool:
        return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    async def get_by_provider_credential(
        self, provider: str, credential: dict[str, str]
    ) -> ApiKey | None:
        keys = await self.list_provider_keys(provider)
        for key in keys:
            if self._credential_equals(key.credential, credential):
                return key
        return None

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
            model.supported_models = key.supported_models
            model.last_refreshed_at = key.last_refreshed_at
            model.cached_available = key.cached_available
            model.cached_quota_available = key.cached_quota_available
            model.cached_capacity_score = key.cached_capacity_score

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

    async def list_keys_needing_refresh(
        self, cutoff: datetime, provider: str | None = None
    ) -> list[ApiKey]:
        async with self._read_factory() as session:
            stmt = select(ApiKeyModel).where(
                (ApiKeyModel.last_refreshed_at.is_(None))
                | (ApiKeyModel.last_refreshed_at < cutoff)
            )
            if provider:
                stmt = stmt.where(ApiKeyModel.provider == provider)
            stmt = stmt.order_by(ApiKeyModel.provider, ApiKeyModel.id)
            result = await session.execute(stmt)
            return [self._to_entity(row) for row in result.scalars().all()]

    async def claim_refresh(self, key_id: str, now: datetime, max_age_seconds: int) -> bool:
        """Atomically claim refresh. Only one process per key wins."""
        cutoff = now - timedelta(seconds=max_age_seconds)
        async with self._write_factory() as session:
            stmt = (
                update(ApiKeyModel)
                .where(ApiKeyModel.id == key_id)
                .where(
                    (ApiKeyModel.last_refreshed_at.is_(None))
                    | (ApiKeyModel.last_refreshed_at < cutoff)
                )
                .values(last_refreshed_at=now)
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

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
            supported_models=model.supported_models or [],
            last_refreshed_at=model.last_refreshed_at,
            cached_available=model.cached_available,
            cached_quota_available=model.cached_quota_available,
            cached_capacity_score=model.cached_capacity_score,
        )
