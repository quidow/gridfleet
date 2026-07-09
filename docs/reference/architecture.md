# System Architecture

GridFleet uses a host-first orchestration model to manage Appium workflows. Its responsibilities are split across four major components: the backend control plane, the host agent, the WebDriver router, and the frontend dashboard.

## 1. Backend Control Plane (FastAPI + Postgres)

The backend is a multi-worker stateless group of HTTP API servers. State is stored entirely in PostgreSQL. 

### The Scheduler Process and Background Loops
Background maintenance loops run in a single dedicated **scheduler process** — in production the `backend-scheduler` Compose service (one worker, `GRIDFLEET_RUN_BACKGROUND_LOOPS=true`); in local or single-container runs the API process itself (the flag defaults to `true`). A **PostgreSQL advisory lock** (`CONTROL_PLANE_LEADER_LOCK_ID = 6001`, held on a dedicated connection for the process lifetime) is a singleton launch guard against an accidental second loop-runner — **not** a leader election. There is no heartbeat row, watcher, or cross-process preemption. Failover is restart-based: the supervisor (`restart: unless-stopped`) restarts a dead container and it re-acquires the lock on lifespan entry, and an in-process stall watchdog `os._exit(70)`s the scheduler when its loops wedge so the supervisor can restart it. The `app.main` lifespan starts 10 background loops (`host_sweep`, `appium_sweep`, `grid_allocation_reaper`, etc.) that:

- Monitor missing Agent heartbeats.
- Evaluate node health via direct-to-Appium probes.
- Sync stray sessions on Appium that don't belong to the internal state.
- Expire stale router allocation tickets (`grid_allocation_reaper`).
- Transition device maintenance lifecycles.

### Appium node lifecycle

Operator routes and lifecycle paths commit intent to `AppiumNode.desired_state`
plus optional `desired_port` and the `restart_requested_at` restart watermark,
then return in milliseconds; each write is followed by a fire-and-forget wake
poke (`POST /agent/appium-nodes/refresh`). The scheduler's `host_sweep` fetches
`/agent/health` once per host at `general.heartbeat_interval_sec`, applies the
host-liveness verdict first, and passes that same payload to the reconciler for
freshly alive hosts. The reconciler (`app/appium_nodes/services/reconciler*.py`)
is observe-only: it matches the agent's self-reported Appium processes against
desired rows and writes observed columns (`pid`, `active_connection_target`,
health fields) — it never starts, stops, or restarts an agent process itself.
The host agent's own `NodeStateLoop` pulls `GET /agent/appium-nodes/desired`,
diffs it against its locally running processes, and owns start/stop/reconfigure
for every host. The minimum orchestration contract is v3
(`MIN_ORCHESTRATION_CONTRACT_VERSION`, `app/hosts/service.py`): a pre-v3 agent
is rejected at registration with HTTP 426, and any already-registered pre-v3
host is marked offline at scheduler startup. See
`docs/design/04-backend-agent-contract.md` for the full contract.

After the per-host fan-out, `host_sweep` runs its cadence-gated stages
(`stage_due`): node health per alive host (`general.node_check_interval_sec`),
then the cross-host global stages in order — connectivity
(`general.device_check_interval_sec`), host-resource telemetry, hardware
telemetry, and property refresh (each gated by its own interval setting) —
against the host statuses this same sweep cycle just wrote.

The same convergence pass counts orphans. It parses
`appium_processes.running_nodes` from the shared sweep payload and increments a
metric-only detector (`APPIUM_PULL_MODE_ORPHANS_OBSERVED`) for agent-side
processes that no DB row claims by `(connection_target, port)` — the agent
already stopped them locally as part of its own pull-and-reap; the backend
stops nothing.

Loop-level readiness and dashboard gauges now use `host_sweep`. The historical
`heartbeat` and `appium_reconciler` loop-level gauge series end at this release;
heartbeat ping metrics and `APPIUM_RECONCILER_*` concern metrics retain their names.

> See [intents.md](./intents.md) for the commands-plus-facts intent model, the
> decision ladder, and the per-source payload table.

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

The scheduler's `appium_sweep` is the single origin for scheduled direct-to-Appium session traffic. Every cycle runs session observation first, including liveness checks, orphan cleanup, idle reaping, and the stale `stop_pending` backstop. It then scans for due session-viability probes no more than once every 60 seconds. Per-device due timestamps remain in the control-plane state store, so doorbell churn does not bunch probes together. Manual, recovery, and verification probes still bypass the sweep.

Session-viability and device-verification probes create a short-lived session directly against the device's Appium server, with no Grid involved. Each probe is persisted as a single terminal `Session` row by its caller via `app.sessions.service_probes.record_probe_session`. Probe rows carry `test_name == "__gridfleet_probe__"` and a `requested_capabilities["gridfleet:probeCheckedBy"]` source attribution (`scheduled`, `manual`, `recovery`, or `verification`).

The allocation reaper still rings the session-sync doorbell after it frees a device, which wakes `appium_sweep` for a prompt observation pass. The `SESSION_SYNC_WAKE_SOURCE_TOTAL` metric keeps its historical name so existing dashboards remain continuous.

Because these probes — along with the session-observation sweep and force-terminate — open HTTP connections straight to `http://{host.ip}:{appium_port}` (via `app.grid.appium_direct`), the **backend (manager) must have direct network reach to the Appium port range on every device host**, the same requirement the router has for WebDriver proxying. Both the backend and the router need this edge; test clients and operators do not. See `docs/guides/security.md`.

Probe rows are excluded from success-rate, throughput, utilization, error breakdown, and heatmap analytics via the existing `exclude_non_test_sessions` / `exclude_non_success_metric_sessions` filters keyed on `test_name`. Probe persistence does **not** emit `session.started` or `session.ended` events — the event stream sees no probe traffic. Operators surface probes on the Sessions page via the `include_probes` query parameter (off by default). Probes have their own retention window via `retention.probe_sessions_days` (default 7 days).

PostgreSQL 18-specific primitives are part of the backend contract. Append-heavy internal rows use database-side UUIDv7 defaults for locality-friendly IDs. Queryable JSON payloads use JSONB with targeted GIN indexes. Device search uses PostgreSQL full-text search over operator-visible identity fields. Reservation cooldown overlap is enforced with a GiST exclusion constraint over a generated `tstzrange`. JSONB fields that are read as whole payloads, such as `software_versions`, job payloads, and event details, intentionally do not have GIN indexes until code paths query inside them.

The PostgreSQL 18 migration is a fresh baseline. Environments that have already run the squashed baseline must rebuild or be migrated manually; editing the baseline does not apply these changes to an already-stamped database.

## 2. Host Agent 

Agents run on physical lab hosts or VMs where devices are attached. Unlike the centralized Backend, Agents run on the 'edge' and govern physical connections.

- **Discovery**: Runs pack-aware probes and adapters, then reports discovered candidates through manager-owned intake flows.
- **Appium Process Management**: The Agent isolates each device by spawning standalone Appium server processes attached to that device's UDID/Serial. The agent runs no Grid relay node; WebDriver traffic reaches Appium via the router (below).
- **Health Checks**: Monitors ADB connectivity and driver viability, terminating Appium processes gracefully if the physical device goes offline.

## 3. WebDriver Router (Rust / Pingora)

The router (`router/`) is a standalone Rust binary that listens on `:4444` and replaces the Selenium Grid hub. For each incoming W3C `POST /session` it calls the backend's internal grid API (`/internal/grid/*`) to allocate and confirm a device, then proxies that session's WebDriver commands directly to the allocated device's Appium server. Subsequent commands on an established session are routed by session id to the same Appium upstream. The backend owns allocation, queueing, and capability matching; the router owns request forwarding. It is configured purely via `GRIDFLEET_ROUTER_*` env vars (see `docs/reference/environment.md`).

## 4. Frontend Operator Dashboard

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
5. The reconciler drives the Agent to start Appium for that device.
6. A CI runner makes a reservation via `/api/runs`.
7. Testing traffic is sent to the router (`http://localhost:4444`), which allocates a matching device via the backend internal grid API and proxies WebDriver traffic directly to that device's local Appium server.
