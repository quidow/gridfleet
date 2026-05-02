# Changelog — GridFleet Backend

All notable changes to the GridFleet backend (FastAPI manager, control plane) are documented here.

## Unreleased

### Features

- Add `GRIDFLEET_AGENT_RECOMMENDED_VERSION` setting and expose `recommended_agent_version` / `agent_update_available` fields on the host API, enabling upgrade awareness for connected agents.
- Add configurable terminal WebSocket scheme (`GRIDFLEET_TERMINAL_WS_SCHEME`).

### Fixes

- Bracket-wrap IPv6 addresses in agent terminal URLs so `ws://[::1]:5100/...` is valid.
- Close drain-transition race by committing draining state before `try_complete_drain`, preventing concurrent `assert_runnable` from starting new work during a drain.

## 0.1.0 — Initial Public Preview

- Initial public preview baseline for the GridFleet control plane backend.
- FastAPI manager with async SQLAlchemy + Postgres, Alembic migrations, and leader-owned background loops.
- Hardened production compose defaults around authentication and host approval.
- Added CI, security scanning, and dependency update workflows.
