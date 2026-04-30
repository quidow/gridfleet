#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_BIN="${COMPOSE_BIN:-docker}"
COMPOSE_SUBCOMMAND="${COMPOSE_SUBCOMMAND:-compose}"
COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/docker/docker-compose.prod.yml}"
COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE:-${REPO_ROOT}/docker/.env}"
BACKUP_DIR="${BACKUP_DIR:-${REPO_ROOT}/backups/postgres}"
BACKUP_PREFIX="${BACKUP_PREFIX:-gridfleet-postgres}"
RETENTION_COUNT="${RETENTION_COUNT:-7}"
POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"

usage() {
    cat <<EOF
Usage: $(basename "$0")

Creates a gzip-compressed PostgreSQL dump from the production compose stack and
stores companion metadata with the alembic revision and key table counts.

Overrides:
  COMPOSE_BIN           Default: docker
  COMPOSE_SUBCOMMAND    Default: compose
  COMPOSE_FILE          Default: docker/docker-compose.prod.yml
  COMPOSE_ENV_FILE      Default: docker/.env
  BACKUP_DIR            Default: backups/postgres
  BACKUP_PREFIX         Default: gridfleet-postgres
  RETENTION_COUNT       Default: 7
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

if [[ "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

command -v "${COMPOSE_BIN}" >/dev/null 2>&1 || {
    echo "Missing compose binary: ${COMPOSE_BIN}" >&2
    exit 1
}

mkdir -p "${BACKUP_DIR}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_path="${BACKUP_DIR}/${BACKUP_PREFIX}-${timestamp}.sql.gz"
metadata_path="${backup_path%.sql.gz}.meta"

postgres_user="$(service_env "${POSTGRES_SERVICE}" POSTGRES_USER)"
postgres_db="$(service_env "${POSTGRES_SERVICE}" POSTGRES_DB)"

if [[ -z "${postgres_user}" || -z "${postgres_db}" ]]; then
    echo "Could not read POSTGRES_USER/POSTGRES_DB from the running ${POSTGRES_SERVICE} container." >&2
    echo "Start the production stack first: cd docker && docker compose -f docker-compose.prod.yml up -d" >&2
    exit 1
fi

echo "Creating backup at ${backup_path}"
compose exec -T "${POSTGRES_SERVICE}" sh -lc '
    export PGPASSWORD="${POSTGRES_PASSWORD:-}"
    pg_dump \
        --no-password \
        --clean \
        --if-exists \
        --no-owner \
        --no-privileges \
        -U "${POSTGRES_USER:?}" \
        -d "${POSTGRES_DB:?}"
' | gzip > "${backup_path}"

alembic_revision="$(query_scalar "SELECT version_num FROM alembic_version LIMIT 1;")"

{
    echo "backup_created_at=${timestamp}"
    echo "database=${postgres_db}"
    echo "alembic_revision=${alembic_revision}"
} > "${metadata_path}"

for table_name in devices hosts sessions test_runs runtime_events webhooks webhook_deliveries; do
    count="$(query_scalar "SELECT COUNT(*) FROM ${table_name};")"
    echo "row_count.${table_name}=${count}" >> "${metadata_path}"
done

echo "Writing metadata to ${metadata_path}"

mapfile -t backup_files < <(find "${BACKUP_DIR}" -maxdepth 1 -type f -name "${BACKUP_PREFIX}-*.sql.gz" | sort -r)
if (( ${#backup_files[@]} > RETENTION_COUNT )); then
    for old_file in "${backup_files[@]:RETENTION_COUNT}"; do
        rm -f "${old_file}" "${old_file%.sql.gz}.meta"
        echo "Pruned old backup ${old_file}"
    done
fi

echo "Backup complete."
