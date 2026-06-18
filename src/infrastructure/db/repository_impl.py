"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-06-08
@Description: SQLAlchemy Key 仓储实现
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.entities.api_key import ApiKey
from domain.exceptions.domain_exceptions import DuplicateCredentialError
from domain.repositories.key_repository import KeyRepository
from domain.value_objects.key_pool import KeyPool
from domain.value_objects.key_status import KeyStatus
from infrastructure.db.models import ApiKeyModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def credential_fingerprint(credential: dict) -> str:
    normalized = json.dumps(credential, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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

    async def list_provider_pool_keys(self, provider: str, pool: KeyPool) -> list[ApiKey]:
        async with self._read_factory() as session:
            stmt = (
                select(ApiKeyModel)
                .where(ApiKeyModel.provider == provider)
                .where(ApiKeyModel.pool == pool.value)
                .order_by(ApiKeyModel.provider, ApiKeyModel.id)
            )
            result = await session.execute(stmt)
            return [self._to_entity(row) for row in result.scalars().all()]

    async def list_keys(self, provider: str | None = None) -> list[ApiKey]:
        async with self._read_factory() as session:
            stmt = select(ApiKeyModel).order_by(ApiKeyModel.provider, ApiKeyModel.id)
            if provider:
                stmt = stmt.where(ApiKeyModel.provider == provider)
            result = await session.execute(stmt)
            return [self._to_entity(row) for row in result.scalars().all()]

    async def list_pool_keys(self, pool: KeyPool) -> list[ApiKey]:
        async with self._read_factory() as session:
            stmt = (
                select(ApiKeyModel)
                .where(ApiKeyModel.pool == pool.value)
                .order_by(ApiKeyModel.provider, ApiKeyModel.id)
            )
            result = await session.execute(stmt)
            return [self._to_entity(row) for row in result.scalars().all()]

    async def get_key(self, key_id: str) -> ApiKey | None:
        async with self._read_factory() as session:
            model = await session.get(ApiKeyModel, key_id)
            return self._to_entity(model) if model else None

    def _credential_equals(self, a: dict, b: dict) -> bool:
        return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    @staticmethod
    def _is_provider_credential_unique_violation(exc: IntegrityError) -> bool:
        message = str(exc.orig).lower()
        return (
            "uq_api_keys_provider_credential" in message
            or (
                "unique constraint failed" in message
                and "api_keys.provider" in message
                and "api_keys.credential_fingerprint" in message
            )
        )

    async def get_by_provider_credential(
        self, provider: str, credential: dict[str, Any]
    ) -> ApiKey | None:
        keys = await self.list_provider_keys(provider)
        for key in keys:
            if self._credential_equals(key.credential, credential):
                return key
        return None

    async def upsert_key(self, key: ApiKey) -> ApiKey:
        now = utcnow()
        async with self._write_factory() as session:
            try:
                model = await session.get(ApiKeyModel, key.id)
                if model is None:
                    model = ApiKeyModel(id=key.id)
                    session.add(model)

                model.provider = key.provider
                model.pool = key.pool.value
                model.max_concurrent_uses = max(key.max_concurrent_uses, 1)
                model.credential = key.credential
                model.credential_fingerprint = credential_fingerprint(key.credential)
                model.status = key.status.value
                model.quota_used = key.quota_used
                model.success_count = key.success_count
                model.error_count = key.error_count
                model.consecutive_error_count = key.consecutive_error_count
                model.cooldown_failure_rounds = key.cooldown_failure_rounds
                model.rate_limit_rounds = key.rate_limit_rounds
                model.last_report_error_type = key.last_report_error_type
                model.last_used_at = key.last_used_at
                model.cooldown_until = key.cooldown_until
                model.supported_models = key.supported_models
                model.last_refreshed_at = key.last_refreshed_at
                model.updated_at = now
                model.cached_available = key.cached_available
                model.cached_quota_available = key.cached_quota_available
                model.cached_capacity_score = key.cached_capacity_score

                await session.commit()
                await session.refresh(model)
                return self._to_entity(model)
            except IntegrityError as exc:
                await session.rollback()
                if self._is_provider_credential_unique_violation(exc):
                    raise DuplicateCredentialError(
                        f"credential already exists for provider {key.provider}"
                    ) from exc
                raise

    async def touch_key_used(self, key_id: str, now: datetime) -> ApiKey | None:
        async with self._write_factory() as session:
            stmt = (
                update(ApiKeyModel)
                .where(ApiKeyModel.id == key_id)
                .values(last_used_at=now, updated_at=now)
            )
            result = await session.execute(stmt)
            if result.rowcount == 0:
                await session.commit()
                return None
            await session.commit()
        return await self.get_key(key_id)

    async def record_success(self, key_id: str, tokens_used: int, now: datetime) -> ApiKey | None:
        async with self._write_factory() as session:
            model = await session.get(ApiKeyModel, key_id)
            if model is None:
                return None
            model.success_count = (model.success_count or 0) + 1
            model.quota_used = (model.quota_used or 0) + max(tokens_used, 0)
            model.last_used_at = now
            model.updated_at = now
            await session.commit()
        return await self.get_key(key_id)

    async def record_error(self, key_id: str, now: datetime) -> ApiKey | None:
        async with self._write_factory() as session:
            model = await session.get(ApiKeyModel, key_id)
            if model is None:
                return None
            model.error_count = (model.error_count or 0) + 1
            model.last_used_at = now
            model.updated_at = now
            await session.commit()
        return await self.get_key(key_id)

    async def record_error_report_state(self, key: ApiKey, now: datetime) -> ApiKey | None:
        async with self._write_factory() as session:
            stmt = (
                update(ApiKeyModel)
                .where(ApiKeyModel.id == key.id)
                .values(
                    error_count=key.error_count,
                    last_used_at=key.last_used_at,
                    status=key.status.value,
                    cooldown_until=key.cooldown_until,
                    consecutive_error_count=key.consecutive_error_count,
                    cooldown_failure_rounds=key.cooldown_failure_rounds,
                    rate_limit_rounds=key.rate_limit_rounds,
                    last_report_error_type=key.last_report_error_type,
                    updated_at=now,
                )
            )
            result = await session.execute(stmt)
            if result.rowcount == 0:
                await session.commit()
                return None
            await session.commit()
        return await self.get_key(key.id)

    async def record_success_report_state(self, key: ApiKey, now: datetime) -> ApiKey | None:
        async with self._write_factory() as session:
            stmt = (
                update(ApiKeyModel)
                .where(ApiKeyModel.id == key.id)
                .values(
                    success_count=key.success_count,
                    quota_used=key.quota_used,
                    last_used_at=key.last_used_at,
                    consecutive_error_count=key.consecutive_error_count,
                    cooldown_failure_rounds=key.cooldown_failure_rounds,
                    rate_limit_rounds=key.rate_limit_rounds,
                    updated_at=now,
                )
            )
            result = await session.execute(stmt)
            if result.rowcount == 0:
                await session.commit()
                return None
            await session.commit()
        return await self.get_key(key.id)

    async def update_status(
        self,
        key_id: str,
        status: str,
        cooldown_until: datetime | None,
        now: datetime,
    ) -> ApiKey | None:
        async with self._write_factory() as session:
            stmt = (
                update(ApiKeyModel)
                .where(ApiKeyModel.id == key_id)
                .values(status=status, cooldown_until=cooldown_until, updated_at=now)
            )
            result = await session.execute(stmt)
            if result.rowcount == 0:
                await session.commit()
                return None
            await session.commit()
        return await self.get_key(key_id)

    async def update_pool(self, key_id: str, pool: KeyPool) -> ApiKey | None:
        now = utcnow()
        async with self._write_factory() as session:
            stmt = (
                update(ApiKeyModel)
                .where(ApiKeyModel.id == key_id)
                .values(pool=pool.value, updated_at=now)
            )
            result = await session.execute(stmt)
            if result.rowcount == 0:
                await session.commit()
                return None
            await session.commit()
        return await self.get_key(key_id)

    async def update_max_concurrent_uses(
        self, key_id: str, max_concurrent_uses: int
    ) -> ApiKey | None:
        now = utcnow()
        async with self._write_factory() as session:
            stmt = (
                update(ApiKeyModel)
                .where(ApiKeyModel.id == key_id)
                .values(
                    max_concurrent_uses=max(max_concurrent_uses, 1),
                    updated_at=now,
                )
            )
            result = await session.execute(stmt)
            if result.rowcount == 0:
                await session.commit()
                return None
            await session.commit()
        return await self.get_key(key_id)

    async def acquire_runtime_lock(
        self,
        key_id: str,
        owner: str,
        now: datetime,
        ttl_seconds: int,
        reason: str,
    ) -> bool:
        lock_until = now + timedelta(seconds=max(ttl_seconds, 1))
        async with self._write_factory() as session:
            stmt = (
                update(ApiKeyModel)
                .where(ApiKeyModel.id == key_id)
                .where(
                    or_(
                        ApiKeyModel.runtime_lock_owner.is_(None),
                        ApiKeyModel.runtime_lock_until.is_(None),
                        ApiKeyModel.runtime_lock_until <= now,
                        ApiKeyModel.runtime_lock_owner == owner,
                    )
                )
                .values(
                    runtime_lock_owner=owner,
                    runtime_lock_until=lock_until,
                    runtime_lock_reason=reason,
                    updated_at=now,
                )
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount > 0

    async def release_runtime_lock(self, key_id: str, owner: str, now: datetime) -> None:
        async with self._write_factory() as session:
            stmt = (
                update(ApiKeyModel)
                .where(ApiKeyModel.id == key_id)
                .where(ApiKeyModel.runtime_lock_owner == owner)
                .values(
                    runtime_lock_owner=None,
                    runtime_lock_until=None,
                    runtime_lock_reason=None,
                    updated_at=now,
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def update_runtime_snapshot_if_locked(
        self,
        key: ApiKey,
        owner: str,
        now: datetime,
    ) -> ApiKey | None:
        async with self._write_factory() as session:
            try:
                stmt = (
                    update(ApiKeyModel)
                    .where(ApiKeyModel.id == key.id)
                    .where(
                        and_(
                            ApiKeyModel.runtime_lock_owner == owner,
                            ApiKeyModel.runtime_lock_until.is_not(None),
                            ApiKeyModel.runtime_lock_until > now,
                        )
                    )
                )
                stmt = stmt.values(
                    credential=key.credential,
                    credential_fingerprint=credential_fingerprint(key.credential),
                    status=key.status.value,
                    cooldown_until=key.cooldown_until,
                    supported_models=key.supported_models,
                    last_refreshed_at=key.last_refreshed_at,
                    cached_available=key.cached_available,
                    cached_quota_available=key.cached_quota_available,
                    cached_capacity_score=key.cached_capacity_score,
                    consecutive_error_count=key.consecutive_error_count,
                    cooldown_failure_rounds=key.cooldown_failure_rounds,
                    rate_limit_rounds=key.rate_limit_rounds,
                    last_report_error_type=key.last_report_error_type,
                    updated_at=now,
                )
                result = await session.execute(stmt)
                if result.rowcount == 0:
                    await session.commit()
                    return None
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if self._is_provider_credential_unique_violation(exc):
                    raise DuplicateCredentialError(
                        f"credential already exists for provider {key.provider}"
                    ) from exc
                raise
        return await self.get_key(key.id)

    async def update_background_runtime_snapshot_if_locked(
        self,
        key: ApiKey,
        owner: str,
        now: datetime,
    ) -> ApiKey | None:
        async with self._write_factory() as session:
            try:
                stmt = (
                    update(ApiKeyModel)
                    .where(ApiKeyModel.id == key.id)
                    .where(
                        and_(
                            ApiKeyModel.runtime_lock_owner == owner,
                            ApiKeyModel.runtime_lock_until.is_not(None),
                            ApiKeyModel.runtime_lock_until > now,
                            ApiKeyModel.status.notin_(
                                [
                                    KeyStatus.DISABLED_ADMIN.value,
                                    KeyStatus.DISABLED_REPORT.value,
                                ]
                            ),
                        )
                    )
                    .values(
                        credential=key.credential,
                        credential_fingerprint=credential_fingerprint(key.credential),
                        status=key.status.value,
                        cooldown_until=key.cooldown_until,
                        supported_models=key.supported_models,
                        last_refreshed_at=key.last_refreshed_at,
                        cached_available=key.cached_available,
                        cached_quota_available=key.cached_quota_available,
                        cached_capacity_score=key.cached_capacity_score,
                        updated_at=now,
                    )
                )
                result = await session.execute(stmt)
                if result.rowcount == 0:
                    await session.commit()
                    return None
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if self._is_provider_credential_unique_violation(exc):
                    raise DuplicateCredentialError(
                        f"credential already exists for provider {key.provider}"
                    ) from exc
                raise
        return await self.get_key(key.id)

    async def update_report_disabled_runtime_snapshot_if_locked(
        self,
        key: ApiKey,
        owner: str,
        now: datetime,
    ) -> ApiKey | None:
        async with self._write_factory() as session:
            try:
                stmt = (
                    update(ApiKeyModel)
                    .where(ApiKeyModel.id == key.id)
                    .where(
                        and_(
                            ApiKeyModel.runtime_lock_owner == owner,
                            ApiKeyModel.runtime_lock_until.is_not(None),
                            ApiKeyModel.runtime_lock_until > now,
                            ApiKeyModel.status == KeyStatus.DISABLED_REPORT.value,
                        )
                    )
                    .values(
                        credential=key.credential,
                        credential_fingerprint=credential_fingerprint(key.credential),
                        status=key.status.value,
                        cooldown_until=key.cooldown_until,
                        supported_models=key.supported_models,
                        last_refreshed_at=key.last_refreshed_at,
                        cached_available=key.cached_available,
                        cached_quota_available=key.cached_quota_available,
                        cached_capacity_score=key.cached_capacity_score,
                        updated_at=now,
                    )
                )
                result = await session.execute(stmt)
                if result.rowcount == 0:
                    await session.commit()
                    return None
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if self._is_provider_credential_unique_violation(exc):
                    raise DuplicateCredentialError(
                        f"credential already exists for provider {key.provider}"
                    ) from exc
                raise
        return await self.get_key(key.id)

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

    def _to_entity(self, model: ApiKeyModel) -> ApiKey:
        return ApiKey(
            id=model.id,
            provider=model.provider,
            credential=model.credential,
            pool=KeyPool(model.pool or KeyPool.DEFAULT.value),
            max_concurrent_uses=max(model.max_concurrent_uses or 1, 1),
            status=KeyStatus(model.status),
            quota_used=model.quota_used,
            last_used_at=_as_utc(model.last_used_at),
            success_count=model.success_count,
            error_count=model.error_count,
            consecutive_error_count=model.consecutive_error_count or 0,
            cooldown_failure_rounds=model.cooldown_failure_rounds or 0,
            rate_limit_rounds=model.rate_limit_rounds or 0,
            last_report_error_type=model.last_report_error_type,
            cooldown_until=_as_utc(model.cooldown_until),
            supported_models=model.supported_models or [],
            last_refreshed_at=_as_utc(model.last_refreshed_at),
            updated_at=_as_utc(model.updated_at),
            cached_available=model.cached_available,
            cached_quota_available=model.cached_quota_available,
            cached_capacity_score=model.cached_capacity_score,
        )
