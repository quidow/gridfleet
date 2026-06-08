#!/bin/sh
set -eu

if [ "${GRIDFLEET_RUN_MIGRATIONS_ON_START:-true}" = "true" ]; then
    alembic upgrade head
fi

# Leader-owned background loops run only on the elected leader (Postgres advisory
# lock), so extra workers add API-serving capacity, not duplicate loops. Each
# worker opens its own DB pool, so keep GRIDFLEET_UVICORN_WORKERS x
# (DB_POOL_SIZE + DB_MAX_OVERFLOW) under Postgres max_connections.
workers="${GRIDFLEET_UVICORN_WORKERS:-1}"
case "$workers" in
    ''|*[!0-9]*) workers=1 ;;
esac
if [ "$workers" -gt 1 ]; then
    set -- "$@" --workers "$workers"
fi

exec "$@"
