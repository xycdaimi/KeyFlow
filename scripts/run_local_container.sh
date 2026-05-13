#!/usr/bin/env sh
set -eu

python - <<'PY'
import asyncio

from container.container import create_container
from infrastructure.config.settings import get_settings
from infrastructure.db.repository_impl import SqlAlchemyKeyRepository
from infrastructure.db.sqlite_bootstrap import bootstrap_sqlite_database


async def main() -> None:
    settings = get_settings()
    if settings.runtime_mode != "local":
        raise RuntimeError("run_local_container.sh requires KEYFLOW_RUNTIME_MODE=local")
    container = create_container(settings)
    repository = container.resolve(SqlAlchemyKeyRepository)
    await bootstrap_sqlite_database(
        settings.local_sqlite_path,
        repository._write_factory.kw["bind"],
    )
    await repository._write_factory.kw["bind"].dispose()


asyncio.run(main())
PY

python src/worker_main.py &
worker_pid="$!"

uvicorn main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --app-dir src \
  --workers "${UVICORN_WORKERS:-2}" &
api_pid="$!"

shutdown() {
  kill "$api_pid" "$worker_pid" 2>/dev/null || true
  wait "$api_pid" "$worker_pid" 2>/dev/null || true
}

trap shutdown INT TERM

set +e
wait "$api_pid"
status="$?"
shutdown
exit "$status"
