"""One-time DB initialisation script.

Run once after deploying to a fresh environment:
    python scripts/init_db.py

This creates all tables defined in infrastructure.db.models.
For production schema evolution use Alembic migrations instead.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from infrastructure.config.settings import get_settings
from infrastructure.db.models import Base
from infrastructure.db.session import create_session_factory
from sqlalchemy.ext.asyncio import create_async_engine


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_write_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    print("Database tables created successfully.")


if __name__ == "__main__":
    asyncio.run(main())
