"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-07
@Description: Sidecar worker 进程入口
"""
from __future__ import annotations

import asyncio

from application.services.key_service import KeyService
from container.container import create_container
from infrastructure.config.settings import Settings, get_settings
from interfaces.workers.background import run_worker_loop


def build_worker_key_service(settings: Settings) -> KeyService:
    container = create_container(settings)
    return container.resolve(KeyService)


async def _run_worker(settings: Settings) -> None:
    key_service = build_worker_key_service(settings)
    stop_event = asyncio.Event()
    await run_worker_loop(
        key_service,
        settings.background_task_interval_seconds,
        stop_event,
    )


def main() -> None:
    settings = get_settings()
    asyncio.run(_run_worker(settings))


if __name__ == "__main__":
    main()
