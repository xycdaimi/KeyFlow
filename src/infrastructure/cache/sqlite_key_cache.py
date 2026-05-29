"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-29
@Description: SQLite 本地运行模式 Key 分配租约
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text, update

from domain.entities.api_key import ApiKey
from domain.repositories.key_repository import KeyAllocationStore
from domain.value_objects.key_pool import KeyPool
from domain.value_objects.key_status import KeyStatus
from infrastructure.db.models import ApiKeyModel, KeyLeaseModel


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class SqliteKeyCache(KeyAllocationStore):
    def __init__(self, write_engine) -> None:
        self._write_engine = write_engine

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

        async with self._write_engine.connect() as conn:
            try:
                await conn.execute(text("BEGIN IMMEDIATE"))
                await conn.execute(
                    delete(KeyLeaseModel)
                    .where(KeyLeaseModel.provider == provider)
                    .where(KeyLeaseModel.pool == pool.value)
                    .where(KeyLeaseModel.lease_until <= now)
                )
                for key_id in ordered_key_ids:
                    key = (
                        await conn.execute(
                            select(
                                ApiKeyModel.provider,
                                ApiKeyModel.pool,
                                ApiKeyModel.status,
                                ApiKeyModel.cooldown_until,
                                ApiKeyModel.max_concurrent_uses,
                            ).where(ApiKeyModel.id == key_id)
                        )
                    ).mappings().first()
                    if key is None or key["provider"] != provider or key["pool"] != pool.value:
                        continue
                    if not self._is_usable(key["status"], key["cooldown_until"], now):
                        continue

                    lease = (
                        await conn.execute(
                            select(KeyLeaseModel.lease_until, KeyLeaseModel.active_count)
                        .where(KeyLeaseModel.provider == provider)
                        .where(KeyLeaseModel.pool == pool.value)
                        .where(KeyLeaseModel.key_id == key_id)
                        )
                    ).mappings().first()
                    active_count = 0
                    lease_until = None
                    if lease is not None:
                        lease_until = lease["lease_until"]
                        active_count = lease["active_count"] or 0
                    max_concurrent_uses = max(key["max_concurrent_uses"] or 1, 1)
                    if lease_until is not None and _utc(lease_until) <= now:
                        await conn.execute(
                            delete(KeyLeaseModel)
                            .where(KeyLeaseModel.provider == provider)
                            .where(KeyLeaseModel.pool == pool.value)
                            .where(KeyLeaseModel.key_id == key_id)
                        )
                        lease_until = None
                        active_count = 0
                    if active_count >= max_concurrent_uses:
                        continue

                    await self._upsert_lease(
                        conn,
                        provider,
                        pool,
                        key_id,
                        expires_at,
                        now,
                        lease_until,
                        active_count,
                    )
                    await conn.commit()
                    return key_id

                await conn.commit()
                return None
            except Exception:
                await conn.rollback()
                raise

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
        async with self._write_engine.begin() as conn:
            lease = (
                await conn.execute(
                    select(KeyLeaseModel.active_count)
                    .where(KeyLeaseModel.provider == provider)
                    .where(KeyLeaseModel.pool == pool.value)
                    .where(KeyLeaseModel.key_id == key_id)
                )
            ).mappings().first()
            if lease is None or (lease["active_count"] or 0) <= 1:
                await conn.execute(
                    delete(KeyLeaseModel)
                    .where(KeyLeaseModel.provider == provider)
                    .where(KeyLeaseModel.pool == pool.value)
                    .where(KeyLeaseModel.key_id == key_id)
                )
                return
            await conn.execute(
                update(KeyLeaseModel)
                .where(KeyLeaseModel.provider == provider)
                .where(KeyLeaseModel.pool == pool.value)
                .where(KeyLeaseModel.key_id == key_id)
                .values(active_count=(lease["active_count"] or 0) - 1)
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
    async def _upsert_lease(
        conn,
        provider: str,
        pool: KeyPool,
        key_id: str,
        expires_at: datetime,
        now: datetime,
        existing_lease_until: datetime | None,
        active_count: int,
    ) -> None:
        if existing_lease_until is None:
            await conn.execute(
                KeyLeaseModel.__table__.insert().values(
                    key_id=key_id,
                    provider=provider,
                    pool=pool.value,
                    lease_until=expires_at,
                    active_count=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            return
        await conn.execute(
            update(KeyLeaseModel)
            .where(KeyLeaseModel.provider == provider)
            .where(KeyLeaseModel.pool == pool.value)
            .where(KeyLeaseModel.key_id == key_id)
            .values(
                provider=provider,
                pool=pool.value,
                lease_until=expires_at,
                active_count=active_count + 1,
                updated_at=now,
            )
        )
