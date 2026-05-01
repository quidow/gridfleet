# Contributing To GridFleet

Thanks for taking the time to improve GridFleet. This project spans a FastAPI backend, a host agent, a React frontend, Docker deployment, driver packs, and a Python testkit, so focused changes with clear verification are easiest to review.

## Before You Start

- Open an issue for larger behavior changes, security-sensitive work, new driver-pack features, or public API changes.
- Keep changes scoped to one concern.
- Do not include real device identifiers, private hostnames, credentials, database dumps, screenshots from private labs, or customer data.
- Follow the existing architecture: core orchestration stays driver-agnostic, and driver-specific discovery/readiness/lifecycle behavior belongs in driver packs or adapters.

## Development Setup

System prerequisites:

- Python 3.12+
- `uv`
- Node.js 24
- Docker with `docker compose`
- PostgreSQL 16+ for backend tests, either local or through Docker

Bring up the local stack:

```bash
cd docker
docker compose up --build -d
```

Install component dependencies:

```bash
cd backend && uv sync --extra dev
cd ../agent && uv sync --extra dev
cd ../testkit && uv sync --extra dev --extra appium
cd ../frontend && npm ci
```

## Validation

Run the fastest meaningful check for the area you changed before opening a pull request.

Backend:

```bash
cd backend
uv run ruff format --check app/ tests/
uv run ruff check app/ tests/
uv run mypy app/
uv run pytest -q -n auto
```

Agent:

```bash
cd agent
uv run ruff format --check agent_app/ tests/
uv run ruff check agent_app/ tests/
uv run mypy agent_app/
uv run pytest -q
```

Testkit:

```bash
cd testkit && uv run --extra dev --extra appium pytest -q
```

Frontend:

```bash
cd frontend
npm run lint
npm run build
npm run test
npm run test:e2e:mocked
```

Run live frontend e2e only when the change touches backend/frontend contracts, auth/session behavior, live API behavior, or CI orchestration:

```bash
cd frontend
npm run test:e2e:live
```

## Security-Sensitive Contributions

Use extra caution around:

- authentication and CSRF
- host registration and agent trust
- host terminal access
- driver-pack upload, storage, extraction, and adapter loading
- subprocess execution on agent hosts
- Appium/Selenium Grid routing
- backup, restore, logs, and telemetry

If you believe you found a vulnerability, follow [SECURITY.md](SECURITY.md) instead of opening a public issue or pull request.

## Driver-Pack Guidelines

- Keep backend orchestration driver-agnostic.
- Put platform-specific discovery, readiness fields, lifecycle actions, health labels, and capability defaults in driver-pack manifests or adapters.
- Treat uploaded adapters as executable host code.
- Include tests for manifest parsing, adapter behavior, and backend/agent contracts when changing driver-pack behavior.

## Pull Request Expectations

Good pull requests include:

- a concise description of the problem and solution
- linked issue when available
- screenshots or short clips for UI changes
- migration notes for database changes
- deployment notes for config or Docker changes
- the exact validation commands you ran

By submitting a contribution, you agree that your contribution is licensed under the Apache License, Version 2.0.
