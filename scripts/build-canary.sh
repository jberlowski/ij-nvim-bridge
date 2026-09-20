#!/usr/bin/env bash
# Build the canary plugin against the exact IntelliJ in the harness image.
#
# Built inside the container rather than on the host so it compiles against the
# pinned build (HARNESS.md §9) instead of a separately downloaded SDK.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${HARNESS_IMAGE:-ij-nvim-harness:base}"

docker run --rm \
  -v "$ROOT/canary:/src" \
  -v "ij-nvim-gradle-cache:/home/dev/.gradle" \
  -u dev \
  --entrypoint bash \
  "$IMAGE" -lc '
    set -euo pipefail
    cd /src
    gradle --no-daemon -q buildPlugin -PlocalIdePath=/opt/idea
    ls -l build/distributions/
  '
