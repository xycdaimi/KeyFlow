"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-05-13
@Description: SQLite 本地运行模式 Key 分配租约
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text, update

from domain.entities.api_key import ApiKey
from domain.repositories.key_repository import KeyAllocationStore
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

    async def remove_key(self, key_id: str, provider: str) -> None:
        await self.release_key_lease(provider, key_id)

    async def allocate_key(
        self,
        provider: str,
        ordered_key_ids: list[str],
        now: datetime,
        lease_seconds: int = 2,
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
                    .where(KeyLeaseModel.lease_until <= now)
                )

                for key_id in ordered_key_ids:
                    key = (
                        await conn.execute(
                            select(
                                ApiKeyModel.provider,
                                ApiKeyModel.status,
                                ApiKeyModel.cooldown_until,
                            ).where(ApiKeyModel.id == key_id)
                        )
                    ).mappings().first()
                    if key is None or key["provider"] != provider:
                        continue
                    if not self._is_usable(key["status"], key["cooldown_until"], now):
                        continue

                    lease_until = await conn.scalar(
                        select(KeyLeaseModel.lease_until).where(KeyLeaseModel.key_id == key_id)
                    )
                    if lease_until is not None and _utc(lease_until) > now:
                        continue

                    if lease_until is None:
                        await conn.execute(
                            KeyLeaseModel.__table__.insert().values(
                                key_id=key_id,
                                provider=provider,
                                lease_until=expires_at,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                    else:
                        await conn.execute(
                            update(KeyLeaseModel)
                            .where(KeyLeaseModel.key_id == key_id)
                            .values(
                                provider=provider,
                                lease_until=expires_at,
                                updated_at=now,
                            )
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
        ordered_keys: list[ApiKey],
        now: datetime,
        lease_seconds: int = 2,
    ) -> str | None:
        for key in ordered_keys:
            allocated_id = await self.allocate_key(
                key.provider,
                [key.id],
                now,
                lease_seconds=lease_seconds,
            )
            if allocated_id is not None:
                return allocated_id
        return None

    async def release_key_lease(self, provider: str, key_id: str) -> None:
        async with self._write_engine.begin() as conn:
            await conn.execute(
                delete(KeyLeaseModel)
                .where(KeyLeaseModel.provider == provider)
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
