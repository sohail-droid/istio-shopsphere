#!/usr/bin/env bash
# Build and push the three changed images.  Usage: ./scripts/build_push.sh [tag]
set -euo pipefail
TAG="${1:-1.2.0}"
REPO="imastha8"
cd "$(dirname "$0")/../services"

for svc in api-gateway order-service payment-service; do
  docker build -t "$REPO/$svc:$TAG" "./$svc"
  docker push "$REPO/$svc:$TAG"
done
