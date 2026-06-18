"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-06-01
@Description: Docker 容器运行时环境变量加载入口
"""

from __future__ import annotations

import argparse
import os
import sys


DEFAULT_DATABASE_URL = "postgresql+asyncpg://keyflow:keyflow@keyflow-postgres:5432/keyflow"
DEFAULT_REDIS_URL = "redis://keyflow-redis:6379/0"


def load_env_file(path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(f"env file does not exist: {path}")

    with open(path, "r", encoding="utf-8-sig") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ[key] = value


def apply_runtime_defaults(role: str) -> None:
    os.environ.setdefault("PORT", "8001" if role == "gateway" else "8000")
    os.environ.setdefault("UVICORN_WORKERS", "1")

    if role in {"api", "worker"}:
        os.environ.setdefault("DATABASE_URL_READ", DEFAULT_DATABASE_URL)
        os.environ.setdefault("DATABASE_URL_WRITE", DEFAULT_DATABASE_URL)
        os.environ.setdefault("REDIS_URL", DEFAULT_REDIS_URL)


def exec_role(role: str) -> None:
    apply_runtime_defaults(role)

    if role == "gateway":
        os.execvp(
            "uvicorn",
            [
                "uvicorn",
                "gateway.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                os.environ["PORT"],
                "--app-dir",
                "src",
                "--workers",
                os.environ["UVICORN_WORKERS"],
            ],
        )
    if role == "api":
        os.execvp(
            "uvicorn",
            [
                "uvicorn",
                "main:app",
                "--host",
                "0.0.0.0",
                "--port",
                os.environ["PORT"],
                "--app-dir",
                "src",
                "--workers",
                os.environ["UVICORN_WORKERS"],
            ],
        )
    if role == "worker":
        os.execvp("python", ["python", "src/worker_main.py"])

    raise ValueError(f"unsupported role: {role}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("gateway", "api", "worker"))
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()

    load_env_file(args.env_file)
    exec_role(args.role)
    return 0


if __name__ == "__main__":
    sys.exit(main())
