#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f ".env" ]]; then
  cp ".env.example" ".env"
  echo "Created .env from .env.example"
fi

if [[ "${1:-}" == "--no-build" ]]; then
  docker compose --env-file .env -f docker/src/docker-compose.yml up -d
else
  docker compose --env-file .env -f docker/src/docker-compose.yml up -d --build
fi
