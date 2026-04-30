# Runbook: Backend Deploy, Restart, And Rollback

Use this runbook for the manual production compose deployment model shipped in Phase 70.

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

## 3. Post-deploy verification

```bash
curl -s http://localhost:8000/health/live | python -m json.tool
curl -s http://localhost:8000/health/ready | python -m json.tool
curl -s http://localhost:8000/metrics | egrep '^(pending_jobs|active_sessions|background_loop_runs_total|background_loop_errors_total)'
curl -s http://localhost:4444/status | python -m json.tool
curl -I http://localhost:3000/
```

Also inspect recent backend logs:

```bash
cd docker
docker compose --env-file .env -f docker-compose.prod.yml logs --tail=200 backend
```

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
curl -s http://localhost:8000/api/hosts | python -m json.tool
curl -s http://localhost:8000/api/devices?status=offline | python -m json.tool
```

## 6. Close-out

Record:

- deployed revision
- backup file used, if any
- health-check results
- any follow-up work needed after the incident
