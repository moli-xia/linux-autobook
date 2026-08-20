#!/usr/bin/env bash
# Build the multi-architecture linux-autobook image.
#
#   ./docker/build.sh                      # 构建 amd64 + arm64 并推送到默认仓库
#   IMAGE=ghcr.io/me/autobook:1.0 ./docker/build.sh
#   PLATFORMS=linux/amd64 PUSH=0 ./docker/build.sh   # 只构建本机架构并加载到本地
set -Eeuo pipefail

IMAGE="${IMAGE:-ghcr.io/moli-xia/linux-autobook:latest}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
PUSH="${PUSH:-1}"
BUILDER="${BUILDER:-autobook-builder}"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }

# Cross-architecture builds need the QEMU interpreters registered with binfmt.
if [[ "$PLATFORMS" == *,* ]] || [[ "$PLATFORMS" != *"$(docker version --format '{{.Server.Arch}}')"* ]]; then
  docker run --privileged --rm tonistiigi/binfmt --install all >/dev/null 2>&1 || true
fi

docker buildx inspect "$BUILDER" >/dev/null 2>&1 || docker buildx create --name "$BUILDER" --driver docker-container --use
docker buildx use "$BUILDER"

output=(--push)
if [[ "$PUSH" != "1" ]]; then
  if [[ "$PLATFORMS" == *,* ]]; then
    # A local docker image can hold only one architecture; keep the manifest in an OCI archive.
    output=(--output "type=oci,dest=$ROOT/autobook-multiarch.tar")
  else
    output=(--load)
  fi
fi

echo "building $IMAGE for $PLATFORMS"
docker buildx build \
  --platform "$PLATFORMS" \
  --tag "$IMAGE" \
  --file "$ROOT/Dockerfile" \
  "${output[@]}" \
  "$ROOT"
echo "done: $IMAGE"
