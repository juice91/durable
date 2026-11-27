#!/usr/bin/env bash
# Keeps the Compose quick start job (.github/workflows/compose.yml) from filling the Docker engine it runs on, which a
# self-hosted runner shares with everything else on that machine.
#
#   compose_runner.sh require-disk GB   fail unless the engine's disk has GB gigabytes free
#   compose_runner.sh cleanup SIZE      remove this run's stack and images (PROJECT, EAP_IMAGE_TAG), drop build cache
#                                       older than 72 h and cap the rest at SIZE (for example 15GB)
set -euo pipefail

# The stack pulls this image anyway.
PROBE_IMAGE=pgvector/pgvector:0.8.2-pg17-trixie

require_disk() {
  local needed=$1 output available
  # A container's root sits on the engine's data disk, inside whatever VM the engine runs in. --rm also removes the
  # anonymous volume the image declares.
  if ! output=$(docker run --rm --network none --entrypoint df "$PROBE_IMAGE" -Pk /); then
    echo "Cannot read the free disk of the Docker engine; not starting." >&2
    return 1
  fi
  available=$(awk 'NR == 2 { print $4 }' <<<"$output")
  if [[ ! $available =~ ^[0-9]+$ ]]; then
    echo "Cannot read the free disk of the Docker engine; not starting." >&2
    return 1
  fi
  local free=$((available / 1024 / 1024))
  if ((available < needed * 1024 * 1024)); then
    echo "Not starting: the Docker engine has ${free} GB free and this job needs at least ${needed} GB." \
      "Free space first (docker system df shows what holds it)." >&2
    return 1
  fi
  echo "The Docker engine has ${free} GB free (at least ${needed} GB needed)."
}

cleanup() {
  local cap=$1 failed=0 cap_flag=--max-used-space
  # The stack and the images may not exist (the job can fail before it makes them).
  docker compose -p "${PROJECT:?}" down -v --remove-orphans || true
  docker image rm "eap-python:${EAP_IMAGE_TAG:?}" || true
  docker image rm "eap-studio:${EAP_IMAGE_TAG}" || true
  # Which builder's cache is pruned, for the log.
  docker buildx inspect || true
  docker builder prune --force --filter until=72h || failed=1
  # Older buildx calls the cap --keep-storage.
  grep -q -- --max-used-space <<<"$(docker builder prune --help)" || cap_flag=--keep-storage
  docker builder prune --force --all "$cap_flag" "$cap" || failed=1
  docker system df || true
  if ((failed)); then
    echo "Pruning the build cache failed: check the engine's disk by hand." >&2
  fi
  return "$failed"
}

case "${1:-}" in
  require-disk) require_disk "${2:?gigabytes}" ;;
  cleanup) cleanup "${2:?cache size}" ;;
  *)
    echo "usage: compose_runner.sh {require-disk GB | cleanup SIZE}" >&2
    exit 64
    ;;
esac
