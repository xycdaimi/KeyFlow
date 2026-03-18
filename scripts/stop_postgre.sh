#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ "${1:-}" == "--volumes" ]]; then
  docker compose -f docker/postgresql/docker-compose.yml down --volumes
else
  docker compose -f docker/postgresql/docker-compose.yml down
fi
