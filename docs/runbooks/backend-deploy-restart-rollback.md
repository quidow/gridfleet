# Runbook: Backend Deploy, Restart, And Rollback

Use this runbook for the manual production compose deployment model shipped in Phase 70.

When `GRIDFLEET_AUTH_ENABLED=true`, every `/api/*` call below requires HTTP Basic auth with the manager's machine credentials. Export them once and pass with `-u`:

```bash
export GRIDFLEET_TESTKIT_USERNAME="$GRIDFLEET_MACHINE_AUTH_USERNAME"
export GRIDFLEET_TESTKIT_PASSWORD="$GRIDFLEET_MACHINE_AUTH_PASSWORD"
```

`/metrics`, `/docs`, `/redoc`, and `/openapi.json` are also gated when auth is enabled, so they need `-u` too. Only `/health/live` and `/health/ready` stay open and do not need `-u`.

## 1. Pre-deploy checklist

From the repo root:

```bash
bash scripts/backup.sh
cd docker
docker compose --env-file .env -f docker-compose.prod.yml ps
curl -s http://localhost:8000/health/ready | python -m json.tool
```

Do not deploy until:

- the latest backup succeeded
- `/health/ready` is healthy
- the current stack is not already degraded

## 2. Deploy or restart the stack

```bash
cd docker
docker compose --env-file .env -f docker-compose.prod.yml up --build -d
```

If you only need a clean backend restart:

```bash
cd docker
docker compose --env-file .env -f docker-compose.prod.yml restart backend
```

### Releases that drop a database column

Some releases run a migration that **drops** a column. The current example is the device-tags-to-groups release, whose migration `c1a7e4d9b620` removes `devices.tags` in a single step. There is no deprecate-then-drop window: the moment the migration lands, any backend process still running the previous image fails on **every** device query, because SQLAlchemy emits the full column list for `select(Device)`.

This matters because production splits the backend into two services. `backend` owns alembic; `backend-scheduler` sets `GRIDFLEET_RUN_MIGRATIONS_ON_START: "false"` and declares `depends_on: backend: service_healthy`. **`depends_on` orders starts, not stops.** A partial update — `docker compose up -d backend`, or any deploy that recreates `backend` without recreating `backend-scheduler` — leaves the old scheduler running against the new schema. Every background loop that touches a device throws, the stall watchdog `os._exit(70)`s the process, and the supervisor restarts it into the same broken image, indefinitely.

So for these releases:

1. Take a backup first (`bash scripts/backup.sh`). A column drop is not reversible from application code, and the migration's `downgrade` refuses to run.
2. **Recreate `backend` and `backend-scheduler` together.** Use the full-stack command in [Deploy or restart the stack](#2-deploy-or-restart-the-stack), or name both services explicitly:

   ```bash
   cd docker
   docker compose --env-file .env -f docker-compose.prod.yml up --build -d backend backend-scheduler
   ```

   Never deploy one of the pair alone. The same applies to the rollback command in [section 4](#4-roll-back-application-code-without-restoring-the-database) — rolling `backend` back to a pre-migration image against a migrated database reproduces the failure from the other direction, so a rollback across a column-drop release needs a database restore ([section 5](#5-roll-back-data-from-backup)), not a code-only rollback.
3. Confirm the scheduler came back clean, not crash-looping:

   ```bash
   cd docker
   docker compose --env-file .env -f docker-compose.prod.yml ps backend-scheduler
   docker compose --env-file .env -f docker-compose.prod.yml logs --tail=200 backend-scheduler
   ```

   A restart count that keeps climbing, or repeated `UndefinedColumn` errors in the log, means the old image is still running.

#### Drain in-flight jobs that carry a dropped field

Durable jobs enqueued **before** the deploy keep the old payload shape in the database, and a payload is replayed through the current code's Pydantic models. For the tags release, `device_verification` jobs enqueued pre-migration carry `tags` inside `payload["data"]`; `DeviceVerificationCreate` / `DeviceVerificationUpdate` are `extra="forbid"`, so validation raises on replay. Those jobs run with `max_attempts=1`, so they do not retry — they fail once and stay dead, and the devices they were queued for never get verified.

Handle this alongside the migration:

- **Before** the deploy, let the queue drain: check `pending_jobs` on `/metrics` and wait for `device_verification` to reach zero, or stop whatever is enqueuing them.
- **After** the deploy, if any pre-deploy jobs are still queued, expect them to fail permanently. Identify the affected devices and re-trigger verification from the Devices page (or re-import the affected rows) rather than waiting for a retry that will never come.

## 3. Post-deploy verification

```bash
curl -s http://localhost:8000/health/live | python -m json.tool
curl -s http://localhost:8000/health/ready | python -m json.tool
curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/metrics | egrep '^(pending_jobs|active_sessions|background_loop_runs_total|background_loop_errors_total)'
curl -s http://localhost:4444/status | python -m json.tool
curl -I http://localhost:3000/
```

Also inspect recent backend logs:

```bash
cd docker
docker compose --env-file .env -f docker-compose.prod.yml logs --tail=200 backend
```

### Audit for dangling `member_of` references

A race between deleting a static group and adding the first `member_of` reference
to it could leave a dynamic group pointing at a key that no longer exists. The
race is fixed, but a reference written before the fix is not repaired by it, and
it fails **silently**: the group resolves to zero members rather than erroring, so
sessions and runs targeting it queue until timeout with no diagnostic.

Run once after upgrading:

```sql
SELECT d.key AS dynamic_group, missing.key AS missing_reference
FROM device_groups d
CROSS JOIN LATERAL jsonb_array_elements_text(d.filters -> 'member_of') AS missing(key)
WHERE d.filters ? 'member_of'
  AND NOT EXISTS (SELECT 1 FROM device_groups s WHERE s.key = missing.key);
```

Any row is a dynamic group that can never match. Repair by editing the group's
filters to drop or repoint the missing key.

## 4. Roll back application code without restoring the database

If the problem is clearly in the new application build and the schema/data is still valid:

```bash
git checkout PREVIOUS_GOOD_REVISION
cd docker
docker compose --env-file .env -f docker-compose.prod.yml up --build -d backend frontend
```

Re-run the post-deploy verification block immediately afterward.

## 5. Roll back data from backup

Use this only when bad data or an unrecoverable migration/result corrupted the database.

```bash
bash scripts/restore.sh /absolute/path/to/backup.sql.gz --yes
```

After restore, repeat:

```bash
curl -s http://localhost:8000/health/ready | python -m json.tool
curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" http://localhost:8000/api/hosts | python -m json.tool
curl -s -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" 'http://localhost:8000/api/devices?status=offline' | python -m json.tool
```

## 6. Close-out

Record:

- deployed revision
- backup file used, if any
- health-check results
- any follow-up work needed after the incident
