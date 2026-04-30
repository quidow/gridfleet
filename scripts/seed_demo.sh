#!/usr/bin/env bash
set -euo pipefail

# scripts/seed_demo.sh — create gridfleet_demo DB if missing, migrate, seed.
#
# Runs entirely inside the existing docker compose containers, so no host-side
# `psql`, `createdb`, `uv`, or Python toolchain is required. The Postgres
# container provides psql/createdb; the backend container provides uv + the
# `app.seeding` module.
#
# Usage:
#   scripts/seed_demo.sh                # full_demo scenario
#   scripts/seed_demo.sh minimal        # minimal scenario
#   scripts/seed_demo.sh chaos          # error/warning UI paths
#   scripts/seed_demo.sh full_demo --skip-telemetry   # fast iteration
#
# Environment overrides:
#   GRIDFLEET_POSTGRES_CONTAINER (default: auto-detected from docker ps)
#   GRIDFLEET_BACKEND_CONTAINER  (default: auto-detected from docker ps)
#   GRIDFLEET_POSTGRES_USER      (default: gridfleet)
#   GRIDFLEET_DEMO_DB_NAME       (default: gridfleet_demo)

SCENARIO="${1:-full_demo}"
shift || true
EXTRA_ARGS=("$@")

POSTGRES_USER="${GRIDFLEET_POSTGRES_USER:-gridfleet}"
DB_NAME="${GRIDFLEET_DEMO_DB_NAME:-gridfleet_demo}"

die() { echo "error: $*" >&2; exit 1; }

need_docker() {
  command -v docker >/dev/null 2>&1 || die "docker not found on PATH. Install docker or run compose up first."
}

find_container_by_image() {
  # Prints the first running container name whose image matches the given pattern.
  local pattern="$1"
  docker ps --format "{{.Names}}\t{{.Image}}" | awk -v pat="${pattern}" '$2 ~ pat { print $1; exit }'
}

find_container_by_name_pattern() {
  local pattern="$1"
  docker ps --format "{{.Names}}" | awk -v pat="${pattern}" '$0 ~ pat { print $0; exit }'
}

need_docker

POSTGRES_CONTAINER="${GRIDFLEET_POSTGRES_CONTAINER:-$(find_container_by_image 'postgres')}"
[[ -n "${POSTGRES_CONTAINER}" ]] || die "no running postgres container found. Start the stack: cd docker && docker compose up -d postgres"

BACKEND_CONTAINER="${GRIDFLEET_BACKEND_CONTAINER:-$(find_container_by_name_pattern 'backend')}"
[[ -n "${BACKEND_CONTAINER}" ]] || die "no running backend container found. Start the stack: cd docker && docker compose up -d backend"

echo "postgres container: ${POSTGRES_CONTAINER}"
echo "backend container:  ${BACKEND_CONTAINER}"

# 1. Create the demo database if missing (executed inside the postgres container).
if ! docker exec "${POSTGRES_CONTAINER}" psql -U "${POSTGRES_USER}" -tAc \
    "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
  echo "creating database ${DB_NAME}"
  docker exec "${POSTGRES_CONTAINER}" createdb -U "${POSTGRES_USER}" "${DB_NAME}"
fi

# Build the asyncpg URL the backend container will use to reach postgres over the compose network.
DB_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_USER}@postgres:5432/${DB_NAME}"

# 2. Run migrations inside the backend container.
echo "running migrations against ${DB_NAME}"
docker exec -e "GRIDFLEET_DATABASE_URL=${DB_URL}" "${BACKEND_CONTAINER}" \
  uv run alembic upgrade head

# 3. Seed the scenario inside the backend container.
echo "seeding scenario=${SCENARIO}"
docker exec -e "GRIDFLEET_DATABASE_URL=${DB_URL}" "${BACKEND_CONTAINER}" \
  uv run python -m app.seeding --scenario "${SCENARIO}" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}

echo ""
echo "done."
echo "to point the running backend at the demo DB:"
echo "  ./scripts/demo-mode.sh on"
