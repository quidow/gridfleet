#!/bin/sh
set -eu

if [ "${GRIDFLEET_RUN_MIGRATIONS_ON_START:-true}" = "true" ]; then
    alembic upgrade head
fi

# Background loops run in a single process — the dedicated backend-scheduler
# service in prod, the in-process default (GRIDFLEET_RUN_BACKGROUND_LOOPS=true)
# in dev — so extra workers only add API-serving capacity. Each worker opens its
# own DB pool, so keep GRIDFLEET_UVICORN_WORKERS x
# (DB_POOL_SIZE + DB_MAX_OVERFLOW) under Postgres max_connections.
workers="${GRIDFLEET_UVICORN_WORKERS:-1}"
case "$workers" in
    ''|*[!0-9]*) workers=1 ;;
esac
# Only the default uvicorn command takes --workers. Guard so an overridden CMD
# (e.g. `docker run <image> alembic ...` or `docker compose run backend <cmd>`)
# is not handed an unrecognized flag.
if [ "$workers" -gt 1 ] && [ "$1" = "uvicorn" ]; then
    set -- "$@" --workers "$workers"
fi

exec "$@"
