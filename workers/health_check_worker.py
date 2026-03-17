"""Periodic health-check worker.

Runs two recurring tasks:
- Cooldown recovery: scans for keys whose cooldown has expired and
  marks them AVAILABLE again (default: every 30 s).
- Health probe: lightweight liveness log so container orchestrators
  can detect a hung worker (default: every 60 s).

Usage:
    python workers/health_check_worker.py
    HEALTH_CHECK_INTERVAL=60 COOLDOWN_INTERVAL=30 python workers/health_check_worker.py
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from application.services.key_service import KeyService
from container.container import create_container
from infrastructure.config.settings import get_settings
from infrastructure.logging.logger import configure_logging

COOLDOWN_INTERVAL = int(os.environ.get("COOLDOWN_INTERVAL", "30"))
HEALTH_CHECK_INTERVAL = int(os.environ.get("HEALTH_CHECK_INTERVAL", "60"))

log = logging.getLogger("keyflow.worker.health")


async def recover_loop(service: KeyService) -> None:
    while True:
        try:
            recovered = await service.recover_cooldowns()
            log.info("cooldown_recovery count=%s", recovered)
        except Exception as exc:
            log.exception("cooldown recovery error: %s", exc)
        await asyncio.sleep(COOLDOWN_INTERVAL)


async def liveness_loop() -> None:
    while True:
        log.info("worker alive")
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    container = create_container(settings)
    service = container.resolve(KeyService)

    log.info(
        "starting health_check_worker cooldown_interval=%ss liveness_interval=%ss",
        COOLDOWN_INTERVAL,
        HEALTH_CHECK_INTERVAL,
    )

    async with asyncio.TaskGroup() as tg:
        tg.create_task(recover_loop(service))
        tg.create_task(liveness_loop())


if __name__ == "__main__":
    asyncio.run(main())
