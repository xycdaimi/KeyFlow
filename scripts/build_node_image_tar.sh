#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

IMAGE_NAME="${KEYFLOW_NODE_IMAGE_NAME:-keyflow-node}"
IMAGE_TAG="${KEYFLOW_IMAGE_TAG:-prod}"
OUTPUT="${1:-dist/${IMAGE_NAME}-${IMAGE_TAG}.tar}"

mkdir -p "$(dirname "$OUTPUT")"

docker build \
  -f docker/src/Dockerfile \
  -t "${IMAGE_NAME}:${IMAGE_TAG}" \
  .

docker save -o "$OUTPUT" "${IMAGE_NAME}:${IMAGE_TAG}"

echo "Node image saved: $OUTPUT"
echo "Node image tag: ${IMAGE_NAME}:${IMAGE_TAG}"
