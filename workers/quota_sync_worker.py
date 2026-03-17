import asyncio
import logging


async def main() -> None:
    # Provider-side quota sync adapters are intentionally left pluggable.
    logging.getLogger("keyflow.worker.quota").info("quota sync worker placeholder executed")


if __name__ == "__main__":
    asyncio.run(main())
