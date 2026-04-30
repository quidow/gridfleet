#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_BIN="${COMPOSE_BIN:-docker}"
COMPOSE_SUBCOMMAND="${COMPOSE_SUBCOMMAND:-compose}"
COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/docker/docker-compose.prod.yml}"
COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE:-${REPO_ROOT}/docker/.env}"
POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"
BACKEND_SERVICE="${BACKEND_SERVICE:-backend}"
FRONTEND_SERVICE="${FRONTEND_SERVICE:-frontend}"
GRID_SERVICE="${GRID_SERVICE:-selenium-hub}"
REQUIRE_CONFIRM_FLAG="${REQUIRE_CONFIRM_FLAG:---yes}"

usage() {
    cat <<EOF
Usage: $(basename "$0") BACKUP.sql.gz ${REQUIRE_CONFIRM_FLAG}

Restores the production PostgreSQL database from a gzip-compressed dump created
by scripts/backup.sh. This replaces the current database contents.

Overrides:
  COMPOSE_BIN           Default: docker
  COMPOSE_SUBCOMMAND    Default: compose
  COMPOSE_FILE          Default: docker/docker-compose.prod.yml
  COMPOSE_ENV_FILE      Default: docker/.env
EOF
}

compose() {
    local cmd=("${COMPOSE_BIN}" "${COMPOSE_SUBCOMMAND}" "--file" "${COMPOSE_FILE}")
    if [ -f "${COMPOSE_ENV_FILE}" ]; then
        cmd+=("--env-file" "${COMPOSE_ENV_FILE}")
    fi
    "${cmd[@]}" "$@"
}

service_env() {
    local service="$1"
    local name="$2"
    compose exec -T "${service}" sh -lc "printf '%s' \"\${${name}:-}\""
}

query_scalar() {
    local sql="$1"
    compose exec -T "${POSTGRES_SERVICE}" sh -lc '
        export PGPASSWORD="${POSTGRES_PASSWORD:-}"
        psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:?}" -d "${POSTGRES_DB:?}" -tA -c "$1"
    ' sh "${sql}"
}

backend_url() {
    local port_line
    port_line="$(compose port "${BACKEND_SERVICE}" 8000 | tail -n 1)"
    if [[ -z "${port_line}" ]]; then
        echo "http://127.0.0.1:8000"
        return
    fi
    echo "http://${port_line}"
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

if [[ $# -lt 2 || "$2" != "${REQUIRE_CONFIRM_FLAG}" ]]; then
    usage >&2
    echo >&2
    echo "Refusing to continue without the destructive confirmation flag ${REQUIRE_CONFIRM_FLAG}." >&2
    exit 1
fi

backup_path="$1"
metadata_path="${backup_path%.sql.gz}.meta"

[[ -f "${backup_path}" ]] || {
    echo "Backup file not found: ${backup_path}" >&2
    exit 1
}

command -v "${COMPOSE_BIN}" >/dev/null 2>&1 || {
    echo "Missing compose binary: ${COMPOSE_BIN}" >&2
    exit 1
}

echo "Starting database service"
compose up -d "${POSTGRES_SERVICE}"

postgres_user="$(service_env "${POSTGRES_SERVICE}" POSTGRES_USER)"
postgres_db="$(service_env "${POSTGRES_SERVICE}" POSTGRES_DB)"

if [[ -z "${postgres_user}" || -z "${postgres_db}" ]]; then
    echo "Could not read POSTGRES_USER/POSTGRES_DB from the running ${POSTGRES_SERVICE} container." >&2
    exit 1
fi

echo "Stopping backend before restore"
compose stop "${BACKEND_SERVICE}" >/dev/null 2>&1 || true

echo "Dropping and recreating ${postgres_db}"
compose exec -T "${POSTGRES_SERVICE}" sh -lc '
    export PGPASSWORD="${POSTGRES_PASSWORD:-}"
    psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:?}" -d postgres <<SQL
DROP DATABASE IF EXISTS "'"${postgres_db}"'" WITH (FORCE);
CREATE DATABASE "'"${postgres_db}"'";
SQL
'

echo "Restoring ${backup_path}"
gunzip -c "${backup_path}" | compose exec -T "${POSTGRES_SERVICE}" sh -lc '
    export PGPASSWORD="${POSTGRES_PASSWORD:-}"
    psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:?}" -d "${POSTGRES_DB:?}"
'

echo "Running alembic migrations"
compose run --rm --no-deps "${BACKEND_SERVICE}" sh -lc 'uv run alembic upgrade head'

echo "Starting application services"
compose up -d "${GRID_SERVICE}" "${BACKEND_SERVICE}" "${FRONTEND_SERVICE}"

health_url="$(backend_url)/health/ready"
echo "Waiting for backend readiness at ${health_url}"
for _ in {1..30}; do
    if curl -fsS "${health_url}" >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
curl -fsS "${health_url}" >/dev/null

echo "Backend is ready. Current verification counts:"
for table_name in devices hosts sessions test_runs runtime_events webhooks webhook_deliveries; do
    current_count="$(query_scalar "SELECT COUNT(*) FROM ${table_name};")"
    echo "  ${table_name}: ${current_count}"
done

if [[ -f "${metadata_path}" ]]; then
    echo "Comparing restored counts with ${metadata_path}"
    while IFS='=' read -r key expected; do
        [[ "${key}" == row_count.* ]] || continue
        table_name="${key#row_count.}"
        current_count="$(query_scalar "SELECT COUNT(*) FROM ${table_name};")"
        if [[ "${current_count}" != "${expected}" ]]; then
            echo "Row-count mismatch for ${table_name}: expected ${expected}, got ${current_count}" >&2
            exit 1
        fi
    done < "${metadata_path}"
fi

echo "Restore complete."
