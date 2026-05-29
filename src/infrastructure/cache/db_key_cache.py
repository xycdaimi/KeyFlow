"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-29
@Description: SQL 数据库 Key 原子分配租约
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.entities.api_key import ApiKey
from domain.repositories.key_repository import KeyAllocationStore
from domain.value_objects.key_pool import KeyPool
from domain.value_objects.key_status import KeyStatus
from infrastructure.db.models import ApiKeyModel, KeyLeaseModel


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class DatabaseKeyCache(KeyAllocationStore):
    def __init__(self, write_session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._write_factory = write_session_factory

    async def sync_key(self, key: ApiKey, score: float) -> None:
        return None

    async def remove_key(self, key_id: str, provider: str, pool: KeyPool) -> None:
        await self.release_key_lease(provider, pool, key_id)

    async def allocate_key(
        self,
        provider: str,
        pool: KeyPool,
        ordered_key_ids: list[str],
        now: datetime,
        lease_seconds: int = 2,
        allow_leased_fallback: bool = True,
    ) -> str | None:
        if not ordered_key_ids:
            return None

        now = _utc(now)
        expires_at = now + timedelta(seconds=max(lease_seconds, 1))

        async with self._write_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(KeyLeaseModel)
                    .where(KeyLeaseModel.provider == provider)
                    .where(KeyLeaseModel.pool == pool.value)
                    .where(KeyLeaseModel.lease_until <= now)
                )

                for key_id in ordered_key_ids:
                    key = (
                        await session.execute(
                            select(
                                ApiKeyModel.provider,
                                ApiKeyModel.pool,
                                ApiKeyModel.status,
                                ApiKeyModel.cooldown_until,
                                ApiKeyModel.max_concurrent_uses,
                            )
                            .where(ApiKeyModel.id == key_id)
                            .with_for_update()
                        )
                    ).mappings().first()
                    if key is None or key["provider"] != provider or key["pool"] != pool.value:
                        continue
                    if not self._is_usable(key["status"], key["cooldown_until"], now):
                        continue

                    lease = await session.get(KeyLeaseModel, key_id, with_for_update=True)
                    active_count = 0 if lease is None else lease.active_count
                    if lease is not None and _utc(lease.lease_until) <= now:
                        await session.delete(lease)
                        await session.flush()
                        lease = None
                        active_count = 0
                    if active_count >= max(key["max_concurrent_uses"] or 1, 1):
                        continue

                    await self._upsert_lease(session, provider, pool, key_id, expires_at, now, lease)
                    return key_id

        return None

    async def allocate_key_any_provider(
        self,
        pool: KeyPool,
        ordered_keys: list[ApiKey],
        now: datetime,
        lease_seconds: int = 2,
        allow_leased_fallback: bool = True,
    ) -> str | None:
        for key in ordered_keys:
            allocated_id = await self.allocate_key(
                key.provider,
                pool,
                [key.id],
                now,
                lease_seconds=lease_seconds,
                allow_leased_fallback=False,
            )
            if allocated_id is not None:
                return allocated_id
        if not allow_leased_fallback:
            return None
        for key in ordered_keys:
            allocated_id = await self.allocate_key(
                key.provider,
                pool,
                [key.id],
                now,
                lease_seconds=lease_seconds,
                allow_leased_fallback=True,
            )
            if allocated_id is not None:
                return allocated_id
        return None

    async def release_key_lease(self, provider: str, pool: KeyPool, key_id: str) -> None:
        async with self._write_factory() as session:
            async with session.begin():
                lease = await session.get(KeyLeaseModel, key_id, with_for_update=True)
                if lease is None or lease.provider != provider or lease.pool != pool.value:
                    return
                if lease.active_count <= 1:
                    await session.delete(lease)
                    return
                lease.active_count -= 1
                lease.updated_at = datetime.now(timezone.utc)

    @staticmethod
    def _is_usable(status: str, cooldown_until: datetime | None, now: datetime) -> bool:
        if status == KeyStatus.AVAILABLE.value:
            return True
        if status not in {KeyStatus.RATE_LIMITED.value, KeyStatus.COOLDOWN.value}:
            return False
        if cooldown_until is None:
            return False
        return _utc(cooldown_until) <= now

    @staticmethod
    async def _upsert_lease(
        session: AsyncSession,
        provider: str,
        pool: KeyPool,
        key_id: str,
        expires_at: datetime,
        now: datetime,
        existing_lease: KeyLeaseModel | None,
    ) -> None:
        if existing_lease is None:
            session.add(
                KeyLeaseModel(
                    key_id=key_id,
                    provider=provider,
                    pool=pool.value,
                    lease_until=expires_at,
                    active_count=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            try:
                await session.flush()
            except IntegrityError:
                raise
            return
        existing_lease.provider = provider
        existing_lease.pool = pool.value
        existing_lease.lease_until = expires_at
        existing_lease.active_count = (existing_lease.active_count or 0) + 1
        existing_lease.updated_at = now
        await session.flush()
