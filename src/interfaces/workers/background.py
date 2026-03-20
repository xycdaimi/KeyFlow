"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-03-20
@Description: Sidecar worker 后台循环与单次迭代执行
"""
from __future__ import annotations

import asyncio
import logging

from application.services.key_service import KeyService

logger = logging.getLogger(__name__)


async def run_worker_iteration(key_service: KeyService) -> tuple[int, int]:
    recovered = 0
    refreshed = 0
    try:
        recovered = await key_service.recover_cooldowns()
    except Exception as exc:
        logger.warning(
            "event=background_phase_error source=keyflow-worker phase=recover_cooldowns error=%s",
            exc,
        )
    try:
        refreshed = await key_service.refresh_keys()
    except Exception as exc:
        logger.warning(
            "event=background_phase_error source=keyflow-worker phase=refresh_keys error=%s",
            exc,
        )
    return recovered, refreshed


async def run_worker_loop(
    key_service: KeyService,
    interval_seconds: int,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        recovered, refreshed = await run_worker_iteration(key_service)
        if recovered or refreshed:
            logger.info(
                "event=worker_iteration source=keyflow-worker recovered=%s refreshed=%s",
                recovered,
                refreshed,
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            break
