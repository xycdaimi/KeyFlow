#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

IMAGE="${KEYFLOW_GATEWAY_IMAGE:-keyflow-gateway:prod}"
NETWORK="${KEYFLOW_GATEWAY_NETWORK:-keyflow-gateway-network}"
LOG_DRIVER="${KEYFLOW_LOG_DRIVER:-local}"
LOG_MAX_SIZE="${KEYFLOW_LOG_MAX_SIZE:-100m}"
LOG_MAX_FILE="${KEYFLOW_LOG_MAX_FILE:-3}"
REPLACE="false"
LOAD_TAR=""
HOST_PORT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)
      IMAGE="${2:?missing image value}"
      shift 2
      ;;
    --load)
      LOAD_TAR="${2:?missing tar path}"
      shift 2
      ;;
    --host-port)
      HOST_PORT="${2:?missing host port value}"
      shift 2
      ;;
    --port)
      HOST_PORT="${2:?missing port value}"
      shift 2
      ;;
    --replace)
      REPLACE="true"
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

env_value() {
  local file="$1"
  local key="$2"
  local fallback="$3"

  if [[ ! -f "$file" ]]; then
    echo "$fallback"
    return
  fi

  local value
  value="$(grep -E "^${key}=" "$file" | tail -n 1 | cut -d '=' -f 2- || true)"
  value="${value%$'\r'}"
  echo "${value:-$fallback}"
}

ensure_container_absent() {
  local name="$1"
  if docker container inspect "$name" >/dev/null 2>&1; then
    if [[ "$REPLACE" == "true" ]]; then
      docker rm -f "$name" >/dev/null
    else
      echo "Container already exists: $name. Re-run with --replace to recreate it." >&2
      exit 1
    fi
  fi
}

if [[ -n "$LOAD_TAR" ]]; then
  docker load -i "$LOAD_TAR"
fi

if [[ ! -f ".env.gateway" ]]; then
  echo "Missing required env file: .env.gateway" >&2
  exit 1
fi

docker network create "$NETWORK" >/dev/null 2>&1 || true
docker volume create keyflow-gateway-data >/dev/null

PORT="$(env_value .env.gateway PORT 8001)"
HOST_PORT="${HOST_PORT:-$PORT}"

ensure_container_absent keyflow-gateway
docker run -d \
  --name keyflow-gateway \
  --restart unless-stopped \
  --log-driver "$LOG_DRIVER" \
  --log-opt "max-size=$LOG_MAX_SIZE" \
  --log-opt "max-file=$LOG_MAX_FILE" \
  --network "$NETWORK" \
  -p "${HOST_PORT}:${PORT}" \
  -v "$ROOT_DIR/.env.gateway:/app/.env.gateway:ro" \
  -v keyflow-gateway-data:/app/data \
  "$IMAGE" \
  python scripts/container_runtime.py gateway --env-file /app/.env.gateway

docker ps --filter "name=keyflow-gateway" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
