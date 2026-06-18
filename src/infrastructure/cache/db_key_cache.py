"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-06-05
@Description: SQL 数据库 Key 原子分配租约
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domain.entities.api_key import ApiKey
from domain.repositories.key_repository import AllocationLease, KeyAllocationStore
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
        async with self._write_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(KeyLeaseModel)
                    .where(KeyLeaseModel.provider == provider)
                    .where(KeyLeaseModel.pool == pool.value)
                    .where(KeyLeaseModel.key_id == key_id)
                )

    async def allocate_key(
        self,
        provider: str,
        pool: KeyPool,
        ordered_key_ids: list[str],
        now: datetime,
        lease_seconds: int = 2,
        allow_leased_fallback: bool = True,
        lease_id: str | None = None,
    ) -> AllocationLease | None:
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

                    active_count = (
                        await session.execute(
                            select(func.count())
                            .select_from(KeyLeaseModel)
                            .where(KeyLeaseModel.provider == provider)
                            .where(KeyLeaseModel.pool == pool.value)
                            .where(KeyLeaseModel.key_id == key_id)
                            .where(KeyLeaseModel.lease_until > now)
                        )
                    ).scalar_one()
                    if active_count >= max(key["max_concurrent_uses"] or 1, 1):
                        continue

                    allocated_lease_id = lease_id or uuid4().hex
                    await self._insert_lease(session, provider, pool, key_id, allocated_lease_id, expires_at, now)
                    return AllocationLease(key_id=key_id, lease_id=allocated_lease_id)

        return None

    async def allocate_key_any_provider(
        self,
        pool: KeyPool,
        ordered_keys: list[ApiKey],
        now: datetime,
        lease_seconds: int = 2,
        allow_leased_fallback: bool = True,
        lease_id: str | None = None,
    ) -> AllocationLease | None:
        for key in ordered_keys:
            lease = await self.allocate_key(
                key.provider,
                pool,
                [key.id],
                now,
                lease_seconds=lease_seconds,
                allow_leased_fallback=False,
                lease_id=lease_id,
            )
            if lease is not None:
                return lease
        if not allow_leased_fallback:
            return None
        for key in ordered_keys:
            lease = await self.allocate_key(
                key.provider,
                pool,
                [key.id],
                now,
                lease_seconds=lease_seconds,
                allow_leased_fallback=True,
                lease_id=lease_id,
            )
            if lease is not None:
                return lease
        return None

    async def release_key_lease(self, provider: str, pool: KeyPool, key_id: str, lease_id: str) -> None:
        async with self._write_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(KeyLeaseModel)
                    .where(KeyLeaseModel.lease_id == lease_id)
                    .where(KeyLeaseModel.provider == provider)
                    .where(KeyLeaseModel.pool == pool.value)
                    .where(KeyLeaseModel.key_id == key_id)
                )

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
    async def _insert_lease(
        session: AsyncSession,
        provider: str,
        pool: KeyPool,
        key_id: str,
        lease_id: str,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        session.add(
            KeyLeaseModel(
                lease_id=lease_id,
                key_id=key_id,
                provider=provider,
                pool=pool.value,
                lease_until=expires_at,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
