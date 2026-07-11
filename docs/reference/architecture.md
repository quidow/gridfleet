# System Architecture

GridFleet uses a host-first orchestration model to manage Appium workflows. Its responsibilities are split across four major components: the backend control plane, the host agent, the WebDriver router, and the frontend dashboard.

## 1. Backend Control Plane (FastAPI + Postgres)

The backend is a multi-worker stateless group of HTTP API servers. State is stored entirely in PostgreSQL. 

### The Scheduler Process and Background Loops
Background maintenance loops run in a single dedicated **scheduler process** — in production the `backend-scheduler` Compose service (one worker, `GRIDFLEET_RUN_BACKGROUND_LOOPS=true`); in local or single-container runs the API process itself (the flag defaults to `true`). A **PostgreSQL advisory lock** (`CONTROL_PLANE_LEADER_LOCK_ID = 6001`, held on a dedicated connection for the process lifetime) is a singleton launch guard against an accidental second loop-runner — **not** a leader election. There is no heartbeat row, watcher, or cross-process preemption. Failover is restart-based: the supervisor (`restart: unless-stopped`) restarts a dead container and it re-acquires the lock on lifespan entry, and an in-process stall watchdog `os._exit(70)`s the scheduler when its loops wedge so the supervisor can restart it. The `app.main` lifespan starts 6 background loops (`host_sweep`, `appium_sweep`, `durable_job_worker`, `grid_allocation_reaper`, `device_intent_reconciler`, `janitor`) that:

- Derive host liveness from the recency of the agent's consolidated status push.
- Evaluate node health via direct-to-Appium probes.
- Sync stray sessions on Appium that don't belong to the internal state.
- Expire stale router allocation tickets (`grid_allocation_reaper`).
- Transition device maintenance lifecycles (`device_intent_reconciler`).
- Run trivial periodic chores as `stage_due` stages of the `janitor` loop (base tick 15 s): `run_reaper`, `fleet_capacity` (60 s), `pack_drain` backstop (60 s), `data_cleanup` (hourly, skips boot), and the heartbeat-snapshot flush (15 s).

**Scheduling doctrine:** a `BackgroundLoop` per independent lifecycle; `stage_due` stages only as sub-cadences of an owning sweep (host_sweep's partition probe; the janitor's stages above). Stage cadences are plumbing constants, never registry settings. Pack drain is event-driven — session/run release paths call `complete_drain_if_draining` inline so a pack disables on the release commit; the janitor's `pack_drain` stage is only the backstop.

### Appium node lifecycle

Operator routes and lifecycle paths commit intent to `AppiumNode.desired_state`
plus optional `desired_port` and the `restart_requested_at` restart watermark,
then return in milliseconds; each write is followed by a fire-and-forget wake
poke (`POST /agent/appium-nodes/refresh`). Agents push one consolidated
`POST /agent/hosts/status` per host every `AGENT_STATUS_PUSH_INTERVAL_SEC`
(default 10 s) carrying nodes, restart events, start failures, pack status,
host telemetry, and agent version/capabilities — pack status has no separate
channel. The scheduler's `host_sweep` evaluates each host's liveness from that
push's recency (`general.host_offline_after_sec`, default 45 s) rather than
dialing the agent. The push handler commits the liveness stamp first, then
ingests restart events, converges Appium nodes, and folds observations for that
host. A cadence-gated `GET /agent/health`
reachability probe (60 s plumbing constant)
still runs per host as a network-partition diagnostic, but it feeds no state.
The latest push remains in `status_push.host_status` for host diagnostics and
the device-capability active-target fill. The reconciler
(`app/appium_nodes/services/reconciler*.py`) is observe-only: it matches the
agent's self-reported Appium processes against desired rows and writes
observed columns (`pid`, `active_connection_target`, health fields) — it never
starts, stops, or restarts an agent process itself. The host agent's own
`NodeStateLoop` pulls `GET /agent/appium-nodes/desired`, diffs it against its
locally running processes, and owns start/stop/reconfigure for every host. The
minimum orchestration contract is v6
(`MIN_ORCHESTRATION_CONTRACT_VERSION`, `app/hosts/service.py`): a pre-v6 agent
is rejected at registration with HTTP 426, and the same check runs on every
`/agent/hosts/status` push — a stale-contract agent's push is rejected, it
stops stamping `last_heartbeat`, and it reads offline within the recency
window. See `docs/design/04-backend-agent-contract.md` for the full contract.

At push ingest, the handler folds the observation sections (`node_health`,
`device_health`, `device_telemetry`, `device_properties`, `host_telemetry`)
into durable facts after the liveness commit. Restart ingest, Appium-node
convergence, and each fold are contained so an observation failure cannot
erase the heartbeat. `host_sweep` is the silence detector: it emits liveness
edges and the offline cascade from push recency, and runs the cadence-gated
`/agent/health` partition diagnostic (60 s plumbing cadence; feeds no state).
The `device_intent_reconciler` tick GCs expired deny intents and clears elapsed
reservation-row cooldowns before its full reconcile scan. The agent produces
observations locally on
fixed cadences (30/60/300/600 s constants in `agent_app/probes.py`); fact
latency is push-bounded (at most one push interval after a probe fires).

Each fact class has exactly one channel:

| Fact | Channel | Cadence owner |
|---|---|---|
| Host liveness | status-push recency (`last_heartbeat`), computed at read time (`app/hosts/liveness.py`); the stored `Host.status` column is the enrollment axis (`pending`) + the sweep-written event ledger | backend `general.host_offline_after_sec` |
| Appium process inventory + restart events | push `appium_processes` | agent push interval (10 s) |
| Node Appium health | push `node_health` | agent constant (30 s); fold push-bounded (≤ push interval) |
| Device pack health | push `device_health` | agent constant (60 s); fold push-bounded (≤ push interval) |
| Device hardware telemetry | push `device_telemetry` | agent constant (300 s); fold push-bounded (≤ push interval) |
| Device properties | push `device_properties` | agent constant (600 s); fold push-bounded (≤ push interval) |
| Host resource telemetry | push `host_telemetry` | push interval; fold push-bounded (≤ push interval) |
| Pack install/doctor status | push `packs` | agent pack state loop |
| Session liveness / orphans | direct-to-Appium (`appium_sweep`, action channel) | backend |
| Network partition (diagnostic only) | backend dial `/agent/health` | 60 s plumbing cadence |

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

The scheduler's `appium_sweep` is the single origin for scheduled direct-to-Appium session traffic. Every cycle runs session observation first, including liveness checks, orphan cleanup, idle reaping, and the stale `deferred_stop` backstop. It then scans for due session-viability probes no more than once every 60 seconds. Per-device due timestamps remain in the control-plane state store, so doorbell churn does not bunch probes together. Manual, recovery, and verification probes still bypass the sweep.

Session-viability and device-verification probes create a short-lived session directly against the device's Appium server, with no Grid involved. Each probe is persisted as a single terminal `Session` row by its caller via `app.sessions.service_probes.record_probe_session`. Probe rows carry `test_name == "__gridfleet_probe__"` and a `requested_capabilities["gridfleet:probeCheckedBy"]` source attribution (`scheduled`, `manual`, `recovery`, or `verification`).

The allocation reaper still rings the session-sync doorbell after it frees a device, which wakes `appium_sweep` for a prompt observation pass. The `SESSION_SYNC_WAKE_SOURCE_TOTAL` metric keeps its historical name so existing dashboards remain continuous.

Because these probes — along with the session-observation sweep and force-terminate — open HTTP connections straight to `http://{host.ip}:{appium_port}` (via `app.grid.appium_direct`), the **backend (manager) must have direct network reach to the Appium port range on every device host**, the same requirement the router has for WebDriver proxying. Both the backend and the router need this edge; test clients and operators do not. See `docs/guides/security.md`.

Probe rows are excluded from success-rate, throughput, utilization, error breakdown, and heatmap analytics via the existing `exclude_non_test_sessions` / `exclude_non_success_metric_sessions` filters keyed on `test_name`. Probe persistence does **not** emit `session.started` or `session.ended` events — the event stream sees no probe traffic. Operators surface probes on the Sessions page via the `include_probes` query parameter (off by default). Probes have their own retention window via `retention.probe_sessions_days` (default 7 days).

PostgreSQL 18-specific primitives are part of the backend contract. Append-heavy internal rows use database-side UUIDv7 defaults for locality-friendly IDs. Queryable JSON payloads use JSONB with targeted GIN indexes. Device search uses PostgreSQL full-text search over operator-visible identity fields. Reservation cooldown overlap is enforced with a GiST exclusion constraint over a generated `tstzrange`. JSONB fields that are read as whole payloads, such as `software_versions`, job payloads, and event details, intentionally do not have GIN indexes until code paths query inside them.

The PostgreSQL 18 migration is a fresh baseline. Environments that have already run the squashed baseline must rebuild or be migrated manually; editing the baseline does not apply these changes to an already-stamped database.

## 2. Host Agent 

Agents run on physical lab hosts or VMs where devices are attached. Unlike the centralized Backend, Agents run on the 'edge' and govern physical connections.

- **Discovery**: Runs pack-aware probes and adapters, then reports discovered candidates through manager-owned intake flows.
- **Adapter Workers**: Installs verified adapter wheels into the pack runtime and runs each `(pack, release)` adapter in a supervised subprocess over a stdlib-only JSON-lines protocol. A blocking or crashing hook is killed/restarted for that pack without wedging the agent event loop; the uploaded wheel remains an untrusted code-execution surface on the host.
- **Appium Process Management**: The Agent isolates each device by spawning standalone Appium server processes attached to that device's UDID/Serial. The agent runs no Grid relay node; WebDriver traffic reaches Appium via the router (below).
- **Health Checks**: Monitors ADB connectivity and driver viability, terminating Appium processes gracefully if the physical device goes offline.

## 3. WebDriver Router (Rust / Pingora)

The router (`router/`) is a standalone Rust binary that listens on `:4444` and replaces the Selenium Grid hub. For each incoming W3C `POST /session` it calls the backend's internal grid API (`/internal/grid/*`) to allocate and confirm a device, then proxies that session's WebDriver commands directly to the allocated device's Appium server. Subsequent commands on an established session are routed by session id to the same Appium upstream. The backend owns allocation, queueing, and capability matching; the router owns request forwarding. It is configured purely via `GRIDFLEET_ROUTER_*` env vars (see `docs/reference/environment.md`).

### Timeout lattice — cross-component ordered budgets

Same-component budget rules live next to their constants as derived expressions or asserts. Rules that span two components cannot be derived in code; this table is their single home. The **owner** is the side you retune first — the other side then updates its mirror (and this table).

| Budget A | Budget B | Rule | Owner | Enforcement |
| --- | --- | --- | --- | --- |
| Router shared HTTP client timeout (40 s, `router/src/backend.rs`) | Backend allocate long-poll slice (`LONG_POLL_SEC` = 25 s, `backend/app/grid/constants.py`) | A > B, or every quiet allocate poll dies on the client timeout | backend | compile-time `const` assert in `router/src/backend.rs` (mirrored constant) |
| Router confirm retry budget (3 × (10 s + 2 s) = 36 s, `router/src/backend.rs`, `proxy.rs`) | Backend confirm grace (`CONFIRM_GRACE_SEC` = 60 s, `backend/app/grid/allocation.py`) | A < B, or a retried confirm outlives the reaper's grace and the allocation is released mid-confirm | backend | compile-time `const` assert in `router/src/backend.rs` (mirrored constant) |
| Router Appium create timeout (`create_timeout`, `router/src/proxy.rs`) | Backend `grid.claim_window_sec` (registry, floor 30 s) | A ≤ min(proxy_timeout, B − 5 s), or the reaper releases the allocation under an in-flight create | backend (registry) | derived expression + unit tests in `router/src/proxy.rs`; the registry floor keeps the −5 s cap engaged |
| Router activity-flush cadence (10 s, `router/src/tasks.rs`) | Backend session-liveness freshness window (`ACTIVITY_FRESH_WINDOW_SEC` = 30 s, `backend/app/sessions/service_sync.py`) | B = 3 × A — retune together, or live sessions fail the freshness gate and get per-session probes (or worse, idle-reaped) | router | comments on both constants + this row |
| Backend `grid.queue_timeout_sec` (registry) | Backend allocate long-poll slice (`LONG_POLL_SEC` = 25 s) | A > B, or a queued waiter can expire mid-poll | backend | settings invariant in `backend/app/settings/invariants.py` (write-time reject + scheduler boot gate) |
| Agent HTTP keep-alive (630 s, `agent/agent_app/config.py`) | Backend agent-pool idle (`POOL_KEEPALIVE_EXPIRY_SEC` = 60 s, `backend/app/agent_comm/http_pool.py`) | A > B, or the backend pool reuses connections the agent already closed and non-idempotent calls fail | backend | comments on both constants + this row |

(One row is backend↔agent rather than backend↔router; it is the same class of rule and this table is the designated single home, so it lives here too.)

## 4. Frontend Operator Dashboard

The Frontend (`frontend/src`) acts as the single pane of glass for Fleet Operators.

- Built with React + TypeScript + Vite.
- Stays fresh via a server-sent events stream (`/api/events`, mounted once in `Layout`) as the primary channel — it invalidates react-query caches and fires toasts on transitions. Per-hook polling is a labeled safety-net backstop: while the SSE stream is connected each hook relaxes to a 60s poll (`sseAdaptivePolling`), falling back to its 5–30s base interval only when the stream drops.
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
