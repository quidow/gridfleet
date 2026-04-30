# Deployment Guide

This guide covers the shipped deployment paths after Phase 70:

- local development stack
- production compose stack
- host-agent installation
- backup and restore operations

For incident-response playbooks after the system is already unhealthy, use [../runbooks/README.md](../runbooks/README.md).

## 1. Choose The Right Stack

| Use case | Compose file | Purpose |
| --- | --- | --- |
| Local development | `docker/docker-compose.yml` | Fast local bring-up with dev credentials and optional pgAdmin |
| Production-style deployment | `docker/docker-compose.prod.yml` | Restart policies, resource limits, health checks, env-driven config, and persistent Postgres storage |

## 2. Production Manager Deployment

### Prerequisites

- Docker with `docker compose`
- a checked-out copy of this repository on the manager host
- ports `3000`, `4442`, `4443`, `4444`, and `8000` reachable where needed

### Bootstrap

```bash
cd docker
cp .env.example .env
```

Review `.env` before first boot. The most important values are:

- `POSTGRES_PASSWORD`
- `GRIDFLEET_DATABASE_URL`
- `GRIDFLEET_AUTH_USERNAME`, `GRIDFLEET_AUTH_PASSWORD`, `GRIDFLEET_AUTH_SESSION_SECRET`
- `GRIDFLEET_MACHINE_AUTH_USERNAME`, `GRIDFLEET_MACHINE_AUTH_PASSWORD`
- `BACKEND_PORT`
- `FRONTEND_PORT`
- `GRID_HUB_PORT`, `GRID_PUBLISH_PORT`, `GRID_SUBSCRIBE_PORT`

Production auth and host trust:

- keep `GRIDFLEET_AUTH_ENABLED=true` for production-style deployments
- populate `GRIDFLEET_AUTH_USERNAME`, `GRIDFLEET_AUTH_PASSWORD`, `GRIDFLEET_AUTH_SESSION_SECRET`, `GRIDFLEET_MACHINE_AUTH_USERNAME`, and `GRIDFLEET_MACHINE_AUTH_PASSWORD` with deployment-specific secrets before exposing the stack
- keep `GRIDFLEET_AUTH_COOKIE_SECURE=true` behind HTTPS/TLS
- keep `GRIDFLEET_HOST_AUTO_ACCEPT=false` unless the manager and every registering host are on a tightly controlled trusted network

`GRIDFLEET_AUTH_ENABLED=false` is intended only for local development and isolated trusted-lab trials. It should not be used for a production-style deployment or any environment reachable by untrusted clients.

Use [../reference/environment.md](../reference/environment.md) for the full supported env surface and the difference between process env vars versus installer-only helpers.

### Start The Stack

```bash
cd docker
docker compose --env-file .env -f docker-compose.prod.yml up --build -d
```

The production stack runs four services:

| Service | Default published port(s) | Notes |
| --- | --- | --- |
| `frontend` | `3000` | nginx-served operator UI |
| `backend` | `8000` | FastAPI API, readiness, metrics |
| `selenium-hub` | `4442`, `4443`, `4444` | Grid event bus + hub |
| `postgres` | none | Internal-only container on the `internal` network |

### Verify The Stack

```bash
curl -s http://localhost:8000/health/live | python -m json.tool
curl -s http://localhost:8000/health/ready | python -m json.tool
curl -s http://localhost:4444/status | python -m json.tool
curl -I http://localhost:3000/
curl -s -u "$GRIDFLEET_MACHINE_AUTH_USERNAME:$GRIDFLEET_MACHINE_AUTH_PASSWORD" http://localhost:8000/metrics | egrep '^(pending_jobs|active_sessions|background_loop_runs_total|background_loop_errors_total)'
```

### Production Compose Notes

- `postgres` is only attached to the internal network and is not published to the host by default.
- `backend` and `selenium-hub` sit on both the public and internal networks because external agents and test clients must reach them.
- all services use `restart: unless-stopped`
- all services define health checks
- Postgres, backend, frontend, and Grid all define memory/CPU limits
- the compose file configures JSON log drivers with bounded file rotation

## 3. Agent Host Installation

The manager stack does not run host agents inside the production compose file. Agents run on each device host.

Before running the installer on a device host, confirm the machine already satisfies [host-requirements.md](host-requirements.md). The installer can wire up the service and detect common tool locations, but it does not install Java, Node, Android SDKs, or Xcode for you.

### Linux

From the repo root on the device host:

```bash
sudo \
  AGENT_MANAGER_URL=http://MANAGER_IP:8000 \
  AGENT_MANAGER_AUTH_USERNAME=gridfleet-machine \
  AGENT_MANAGER_AUTH_PASSWORD=change-me \
  GRID_HUB_URL=http://MANAGER_IP:4444 \
  GRID_PUBLISH_URL=tcp://MANAGER_IP:4442 \
  GRID_SUBSCRIBE_URL=tcp://MANAGER_IP:4443 \
  bash agent/install.sh
```

Verify:

```bash
curl -s http://localhost:5100/agent/health | python -m json.tool
sudo systemctl status gridfleet-agent
```

The installer writes process environment to `/etc/gridfleet-agent/config.env`. When you provide `AGENT_MANAGER_AUTH_USERNAME` and `AGENT_MANAGER_AUTH_PASSWORD`, it persists them there as optional manager API credentials.

With the recommended production default `GRIDFLEET_HOST_AUTO_ACCEPT=false`, newly registered agents appear as pending hosts in the manager and must be approved by an operator before they can manage devices.

### macOS

From the repo root on the device host:

```bash
sudo \
  AGENT_MANAGER_URL=http://MANAGER_IP:8000 \
  AGENT_MANAGER_AUTH_USERNAME=gridfleet-machine \
  AGENT_MANAGER_AUTH_PASSWORD=change-me \
  GRID_HUB_URL=http://MANAGER_IP:4444 \
  GRID_PUBLISH_URL=tcp://MANAGER_IP:4442 \
  GRID_SUBSCRIBE_URL=tcp://MANAGER_IP:4443 \
  bash agent/install.sh
```

Verify:

```bash
curl -s http://localhost:5100/agent/health | python -m json.tool
launchctl print "gui/$(id -u)/com.gridfleet.agent"
```

The installer writes a LaunchAgent at `~/Library/LaunchAgents/com.gridfleet.agent.plist`.

## 4. Backup And Restore

### Create A Backup

From the repo root on the manager host:

```bash
bash scripts/backup.sh
```

The script:

- connects to the running production Postgres container
- writes a gzip-compressed SQL dump under `backups/postgres/`
- writes a companion `.meta` file with the alembic revision and key table counts
- keeps the newest seven backups by default

Useful overrides:

```bash
BACKUP_DIR=/srv/gridfleet/backups RETENTION_COUNT=14 bash scripts/backup.sh
```

### Schedule Daily Backups

Example cron entry:

```cron
15 2 * * * cd /path/to/gridfleet && bash scripts/backup.sh >> /var/log/gridfleet-backup.log 2>&1
```

### Restore From Backup

```bash
bash scripts/restore.sh /absolute/path/to/gridfleet-postgres-YYYYMMDDTHHMMSSZ.sql.gz --yes
```

The restore script:

- starts Postgres if needed
- stops the backend
- drops and recreates the configured database
- restores the SQL dump
- runs `uv run alembic upgrade head` through the backend image
- restarts the application services
- waits for `/health/ready`
- compares restored row counts to the backup metadata when available

## 5. Local Development Stack

For local-only development:

```bash
cd docker
docker compose up --build -d
```

This uses `docker/docker-compose.yml`, which keeps the old development-friendly defaults and optional `pgadmin` profile.

Main local endpoints:

- UI: `http://localhost:3000`
- Backend API: `http://localhost:8000`
- Selenium Grid: `http://localhost:4444`

## 6. CI And Test Integration

The deployment surface stops at bringing the manager and agents online.

For the supported pytest/plugin/testkit workflow, continue to:

- [ci-integration.md](ci-integration.md)
- [../reference/testkit.md](../reference/testkit.md)
- [../reference/capabilities.md](../reference/capabilities.md)

## 7. Related Ops Docs

- [../runbooks/slow-system.md](../runbooks/slow-system.md)
- [../runbooks/agent-not-connecting.md](../runbooks/agent-not-connecting.md)
- [../runbooks/stuck-devices.md](../runbooks/stuck-devices.md)
- [../runbooks/webhook-delivery-failures.md](../runbooks/webhook-delivery-failures.md)
- [../runbooks/backend-deploy-restart-rollback.md](../runbooks/backend-deploy-restart-rollback.md)
