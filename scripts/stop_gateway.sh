#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f ".env.gateway" ]]; then
  echo "Skip gateway down: .env.gateway not found."
  exit 0
fi

if [[ "${1:-}" == "--volumes" ]]; then
  docker compose -f docker/gateway/docker-compose.yml down --volumes
else
  docker compose -f docker/gateway/docker-compose.yml down
fi
