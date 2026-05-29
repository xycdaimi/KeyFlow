#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f ".env.gateway" ]]; then
  cp ".env.gateway.example" ".env.gateway"
  echo "Created .env.gateway from .env.gateway.example"
fi

if [[ "${1:-}" == "--no-build" ]]; then
  docker compose --env-file .env.gateway -f docker/gateway/docker-compose.yml up -d
else
  docker compose --env-file .env.gateway -f docker/gateway/docker-compose.yml up -d --build
fi
