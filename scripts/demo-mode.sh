#!/usr/bin/env bash
set -euo pipefail

# scripts/demo-mode.sh — toggle the backend container between the dev DB
# (gridfleet) and the demo DB (gridfleet_demo).
#
# Usage:
#   scripts/demo-mode.sh on        # restart backend against gridfleet_demo
#   scripts/demo-mode.sh off       # restart backend against gridfleet
#   scripts/demo-mode.sh status    # print the DB URL the backend is using
#
# Notes:
# * "on" does not seed the demo DB. Run scripts/seed_demo.sh separately when
#   the demo DB needs a fresh fleet.
# * Both modes share the same Postgres container, so switching is just a
#   backend-container restart; no data is lost on either side.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCKER_DIR="${REPO_ROOT}/docker"
BASE_COMPOSE="${DOCKER_DIR}/docker-compose.yml"
DEMO_OVERRIDE="${DOCKER_DIR}/docker-compose.demo.yml"

usage() {
  echo "usage: $0 {on|off|status}" >&2
  exit 2
}

if [[ $# -ne 1 ]]; then
  usage
fi

restart_backend() {
  # Recreate backend + frontend so compose applies the selected backend
  # environment while leaving postgres + selenium-hub untouched.
  ( cd "${DOCKER_DIR}" && docker compose "$@" up -d --build --force-recreate --no-deps backend frontend )
}

backend_container() {
  ( cd "${DOCKER_DIR}" && docker compose -f docker-compose.yml -f docker-compose.demo.yml ps -q backend )
}

case "$1" in
  on)
    echo "switching backend to gridfleet_demo"
    restart_backend -f docker-compose.yml -f docker-compose.demo.yml
    echo ""
    echo "backend now reads from gridfleet_demo."
    echo "background loops: frozen (GRIDFLEET_FREEZE_BACKGROUND_LOOPS=1)."
    echo "seed it with: scripts/seed_demo.sh full_demo"
    ;;
  off)
    echo "switching backend back to gridfleet (dev)"
    restart_backend -f docker-compose.yml
    echo ""
    echo "backend now reads from gridfleet (dev)."
    ;;
  status)
    container_id="$(backend_container)"
    if [[ -z "${container_id}" ]] || ! docker inspect "${container_id}" >/dev/null 2>&1; then
      echo "backend container not running" >&2
      exit 1
    fi
    url="$(docker exec "${container_id}" printenv GRIDFLEET_DATABASE_URL 2>/dev/null || true)"
    if [[ -z "${url}" ]]; then
      echo "GRIDFLEET_DATABASE_URL not set on backend container" >&2
      exit 1
    fi
    echo "${url}"
    case "${url}" in
      */gridfleet_demo) echo "mode: demo" ;;
      */gridfleet)      echo "mode: dev" ;;
      *)                echo "mode: unknown" ;;
    esac
    freeze="$(docker exec "${container_id}" printenv GRIDFLEET_FREEZE_BACKGROUND_LOOPS 2>/dev/null || true)"
    case "${freeze}" in
      1|true|TRUE|True|yes|YES|Yes|on|ON|On) echo "background loops: frozen" ;;
      *)                                      echo "background loops: running" ;;
    esac
    ;;
  *)
    usage
    ;;
esac
