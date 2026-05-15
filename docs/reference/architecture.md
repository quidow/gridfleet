# System Architecture

GridFleet uses a host-first orchestration model to manage Appium and Selenium Grid workflows. Its responsibilities are split across three major layers.

## 1. Backend Control Plane (FastAPI + Postgres)

The backend is a multi-worker stateless group of HTTP API servers. State is stored entirely in PostgreSQL. 

### Advisory Locks and Background Loops
Because multiple Uvicorn/FastAPI workers can run simultaneously (e.g., in a production Compose setup), the backend uses **PostgreSQL Advisory Locks** to ensure exactly one leader evaluates background maintenance tasks. The `app.main` lifespan starts ~10 distinct background loops (heartbeat, session_sync, node_health, device_connectivity, property_refresh, etc.) that:

- Monitor missing Agent heartbeats.
- Evaluate node health via Appium and Grid calls.
- Sync stray sessions on Appium that don't belong to the internal state.
- Transition device maintenance lifecycles.

### Appium node lifecycle

Operator routes and lifecycle paths commit intent to `AppiumNode.desired_state`
plus optional `desired_port`, `transition_token`, and `transition_deadline`,
then return in milliseconds. The leader-elected reconciler
(`app/appium_nodes/services/reconciler*.py`) reads intent each cycle, drives the
host agent's Appium processes (`appium_start` / `appium_stop`), and writes
observed columns (`pid`, `active_connection_target`, health fields). The
reconciler is the primary writer of observed Appium-node process state.

The same loop is the canonical orphan reaper. For each online host, it fetches
`/agent/health`, parses `appium_processes.running_nodes`, and stops agent-side
processes that no DB row claims by `(connection_target, port)`. Reasons surfaced
via metrics include `no_db_row`, `db_state_not_running`, and `port_mismatch`.

Migration history: Phase 1 added the orphan reconciler scaffold; Phase 2 added
desired-state schema; Phase 3 dual-wrote intent beside legacy inline RPC; Phase
4 enabled convergence; Phase 5 dropped the legacy `state` column and exposed
API `effective_state` as the derived read model.

### Data Storage
- Database: Async PostgreSQL via `asyncpg`.
- Tables are strictly schema-typed with `alembic` handling migrations.
- `tags` and hardware detections are JSON fields attached to the `Device` model.
- Process configurations use `GRIDFLEET_` prefixed env vars, while the device configuration mostly delegates to a dynamic Database Settings Registry.

### Probe Sessions

Session viability, node health, and device verification each run a sub-second probe against the Selenium Grid. Each probe is persisted as a single terminal `Session` row by its caller via `app.sessions.service_probes.record_probe_session`, not by the grid session-sync loop. Probe rows carry `test_name == "__gridfleet_probe__"` and a `requested_capabilities["gridfleet:probeCheckedBy"]` source attribution (`scheduled`, `manual`, `recovery`, `node_health`, or `verification`).

Probe rows are excluded from success-rate, throughput, utilization, error breakdown, and heatmap analytics via the existing `exclude_non_test_sessions` / `exclude_non_success_metric_sessions` filters keyed on `test_name`. Probe persistence does **not** emit `session.started` or `session.ended` events — webhooks and the event stream see no probe traffic. Operators surface probes on the Sessions page via the `include_probes` query parameter (off by default). Probes have their own retention window via `retention.probe_sessions_days` (default 7 days).

PostgreSQL 18-specific primitives are part of the backend contract. Append-heavy internal rows use database-side UUIDv7 defaults for locality-friendly IDs. Queryable JSON payloads use JSONB with targeted GIN indexes. Device search uses PostgreSQL full-text search over operator-visible identity fields. Reservation cooldown overlap is enforced with a GiST exclusion constraint over a generated `tstzrange`. JSONB fields that are read as whole payloads, such as `software_versions`, job payloads, and event details, intentionally do not have GIN indexes until code paths query inside them.

The PostgreSQL 18 migration is a fresh baseline. Environments that have already run the squashed baseline must rebuild or be migrated manually; editing the baseline does not apply these changes to an already-stamped database.

## 2. Host Agent 

Agents run on physical lab hosts or VMs where devices are attached. Unlike the centralized Backend, Agents run on the 'edge' and govern physical connections.

- **Discovery**: Runs pack-aware probes and adapters, then reports discovered candidates through manager-owned intake flows.
- **Appium Process Management**: The Agent isolates each device by spawning standalone Appium server processes attached to that device's UDID/Serial.
- **Selenium Grid Registration**: Once Appium is healthy, the Agent starts an in-process Python Grid Node service. The service registers with the central Selenium Hub over the Grid event bus and reverse-proxies WebDriver traffic to local Appium.
- **Health Checks**: Monitors ADB connectivity and driver viability, terminating Appium processes gracefully if the physical device goes offline.

## 3. Frontend Operator Dashboard

The Frontend (`frontend/src`) acts as the single pane of glass for Fleet Operators.

- Built with React + TypeScript + Vite.
- Continuously polls API endpoints (5-15s intervals) to present real-time readiness matrices.
- Serves as the interface for Device Onboarding/Intake (where discovered devices are promoted into the active fleet).
- Exposes bulk actions and run/reservation overrides.

## Typical Event Flow (Registration & Run)

1. A device is plugged into a lab machine with the Agent installed.
2. The Agent discovers it through the relevant driver-pack probe and reports candidates to the manager-owned discovery flow.
3. The Operator views the Intake drawer in the Dashboard and "Registers" the device.
4. The Backend records desired Appium-node intent for that device.
5. The reconciler drives the Agent to start Appium and attach the complementary Grid Node to the Hub.
6. A CI runner makes a reservation via `/api/runs`.
7. Testing traffic is sent directly to the Hub (`http://localhost:4444`), where Selenium routing matches it by capabilities to the Python Grid Node, which proxies WebDriver traffic to the local Appium server and device.
