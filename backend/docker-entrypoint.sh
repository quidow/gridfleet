#!/bin/sh
set -eu

if [ "${GRIDFLEET_RUN_MIGRATIONS_ON_START:-true}" = "true" ]; then
    alembic upgrade head
fi

exec "$@"
