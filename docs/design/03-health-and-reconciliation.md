# Doc 3: Health & Reconciliation Loops

> Rationale and invariants for the scheduler-owned background loops. The loop
> roster lives in code (`_build_leader_loop_tasks`, `backend/app/main.py`); the
> table below is pinned to it in both directions by
> `backend/tests/contracts/test_design_doc_parity.py`.

GridFleet's API workers are stateless, and API mutations can run on any worker. Background maintenance loops run in one scheduler process. Production uses the `backend-scheduler` Compose service with one worker; local and single-container runs can use the API process because `GRIDFLEET_RUN_BACKGROUND_LOOPS` defaults to `true`.

## Scheduler process and launch guard

The scheduler acquires PostgreSQL advisory lock `6001` during lifespan startup (`backend/app/core/leader/advisory.py`, singleton `control_plane_leader`) and holds it on a dedicated connection for the process lifetime. This is a launch guard against an accidental second loop runner, **not** a leader election: there is no heartbeat row, watcher, write fence, or cross-process preemption. Failover is restart-based — the supervisor restarts a dead scheduler, which re-acquires the lock at its next startup; PostgreSQL releases the lock when the holding connection dies. A process that cannot take the lock logs `background_loops_skipped_lock_held` and runs no loops; a process with `GRIDFLEET_RUN_BACKGROUND_LOOPS=false` never tries.

The advisory lock does not serialize loop writes against API writes; API mutators bypass it entirely. The device row lock (Doc 1) is what protects each device's write window across the scheduler and API workers.

## Stall detection and restart

Every roster loop reports cycle observations through `observe_background_loop`; the janitor's `flush` stage persists the snapshots for operator visibility. A watchdog (`_scheduler_stall_watchdog`, `backend/app/main.py`) checks for stale loops every 60 seconds with an additional 600-second grace period and calls `os._exit(70)` on a wedged loop so the supervisor restarts the scheduler. A fully wedged event loop starves the watchdog too — Prometheus loop-staleness alerts cover that case.

## Loop registry

One `BackgroundLoop` per independent lifecycle. Cadences, read sets, and write sets live with each loop's code; this table names each loop's single responsibility only.

| Loop | Owns |
| --- | --- |
| `host_sweep_loop` | Host liveness from push recency (`evaluate_host` in `app/hosts/liveness.py` is the single `Host.status` edge detector), the offline cascade, node convergence from the latest pushed snapshot, the inline observation folds (device health, host/device telemetry, device properties), and the cadence-gated `/agent/health` partition diagnostic |
| `appium_sweep_loop` | The direct-to-Appium session observation pass (liveness probes + orphan-session kill) and the scheduled session-viability pass behind a 60 s scan throttle |
| `durable_job_worker_loop` | Durable `jobs` table execution |
| `grid_allocation_reaper_loop` | Queue-ticket expiry and crash-orphaned pending `Session` rows |
| `device_intent_reconciler_loop` | Expired deny-intent GC, reservation cooldown clears, and the operational-state edge detector: it compares the read-time projection with `Device.operational_state_last_emitted`, queues `device.operational_state_changed`, and advances the ledger — it does not write current state (Doc 1) |
| `janitor_loop` | Housekeeping stages `run_reaper`, `fleet_capacity`, `pack_drain` (backstop only — release paths complete drains inline), `data_cleanup`, `flush`; stage roster in `_build_janitor` (`backend/app/main.py`), stage cadences are plumbing constants, never registry settings |
| `status_fold_loop` | The level-triggered `node_health` observation-application, folded off the request path: reads each host's latest pushed snapshot and applies the `node_health` section whose ingest-stamped revision exceeds the host's `observation_applied` watermark, under the two-axis guard, with per-host and per-node containment |

Scheduling doctrine for new work: a `BackgroundLoop` per independent lifecycle; stage-due stages only as sub-cadences of an owning sweep. Observation arrives via the agent's consolidated status push (`POST /agent/hosts/status`); the backend dials an agent only to act, never to observe. `docs/reference/architecture.md` documents the push/dial split.

During the partial status-fold rollout, `device_health` remains synchronous and intentionally stays outside ingest dedup/revision stamping. Its current path still performs dials and actions, so exact pushes must remain retryable after a contained inline-fold failure. It joins the guarded cursor only when the later facts-only reconciler and durable-remediation phase lands.

## The tri-state probe pattern

Every signal derived from an agent or a device's Appium node is projected to `ProbeResult` (`app/agent_comm/probe_result.py`):

```text
ack           : definitive success
refused       : definitive failure (the probe answered "no")
indeterminate : transport error, HTTP error response, or open circuit
```

Consumers **must** branch on `result.status` explicitly, and `indeterminate` must early-return: no health-column change, no failure-window advance, no availability flip. This is the invariant that keeps a transient agent blip from flapping device health. The pushed `node_health` section carries the same tri-state liveness signal the agent's local probe produced; the scheduled viability pass in `appium_sweep` is the deep tier — it creates a real WebDriver session directly against the device's Appium node, claiming with a `Session` row from birth (`docs/reference/architecture.md`, Probe Sessions).

## Idempotency rules

Loops can run repeatedly against the same device without ill effect, provided they obey:

1. **Conditional writes only.** The edge detector emits only when the projected value differs from `operational_state_last_emitted`; `app.devices.services.health` only queues `device.health_changed` when the derived public summary's status snapshot changes.
2. **Facts have one home.** Device checks, session viability, emulator state, node lifecycle, transient node-health detail, and node failure timestamps live in typed columns. Readers compose them on demand.
3. **Failure windows live on the node row, not in memory.** `AppiumNode.health_failing_since` survives a scheduler restart, so an active episode is neither lost nor double-counted on replayed observations.

## Where health state lives

Every public health fact has exactly one durable home:

- `Device.device_checks_*`: the `host_sweep` device-health fold
- `Device.session_viability_*`: the scheduled viability pass in `appium_sweep`
- `Device.emulator_state`: the `host_sweep` device-health fold
- `AppiumNode.desired_state`: `write_desired_state` (Doc 2)
- `AppiumNode.health_running` / `AppiumNode.health_state`: `apply_node_state_transition`
- `AppiumNode.health_failing_since`: the `status_fold_loop` node-health fold

`build_public_summary(device)` (`app.devices.services.health_view`, re-exported via `app.devices.services.health`) is the only consumer projection; readers call it on demand. There is no eventually consistent health layer to drift.

`control_plane_state_store` still exists, but only for ephemeral loop coordination and diagnostics — never as canonical device/node state:

| Namespace | Owner | Purpose |
| --- | --- | --- |
| `status_push.host_status` | `POST /agent/hosts/status` ingest (any worker); read by `status_fold_loop`, `host_sweep_loop`, and host diagnostics | latest consolidated agent status push per host (Appium processes, health sections, host telemetry) — the single snapshot source; guarded health sections become eligible only after post-convergence stamping |
| `heartbeat.appium_restart_sequence` | `host_sweep_loop` | last ingested local restart event sequence per host |
| `connectivity.previously_offline` | `host_sweep` connectivity fold | remembers why a reconnect is treated as recovery rather than first startup |
| `hardware_telemetry.state` | `host_sweep` hardware-telemetry fold | stale/fresh telemetry bookkeeping |
| `host_sweep.observation_fold` | `host_sweep` observation folds | per-host stamp watermark per pushed section, an optimization that skips redundant work; folds remain idempotent |
| `session_viability.state` | `appium_sweep` viability pass | cadence bookkeeping for deeper session probes (the in-flight guard is the probe's own `Session` row since WS-16.1) |

## Escalation

Detection debounce — failure windows such as `general.node_fail_window_sec`, ping duration windows, attempt budgets — is per-observer and only decides when a failure event is real. Everything after detection (backoff, node-process directives, review promotion, reset semantics) is owned by the shared remediation ladder derived from the append-only `device_remediation_log` (`app/lifecycle/services/remediation_log.py`). See `docs/reference/device-lifecycle.md`, "Shared remediation escalation ladder" — that reference is canonical; this doc deliberately does not restate it.

## What a new loop must implement

1. Add a `BackgroundLoop` subclass (`app/core/background_loop.py`) under the owning domain's `services/` package, implementing `_session_factory`, `_interval`, and `_run_cycle` and setting the `loop_name` / `cycle_failed_message` class vars. The base class wraps each cycle in `observe_background_loop(...)` for metrics. If the work is a sub-cadence of an existing lifecycle, add a stage to the owning sweep instead (`JanitorStage` is the model) — never a bare `asyncio.create_task`.
2. Add it to `_build_leader_loop_tasks` in `app/main.py` **and** to the roster table above — the parity test fails otherwise, in both directions.
3. Read settings via `settings_service.get(...)`; operator-tunable cadences go in the registry (`app/settings/registry.py`), stage sub-cadences stay plumbing constants.
4. Acquire device row locks via `device_locking.lock_device` for any device-state mutation.
5. Use the tri-state probe pattern for any agent or Appium-node signal.
6. Route health writes through `app.devices.services.health` (`DeviceHealthService`) so locks, facts, and `device.health_changed` stay centralized.
7. Expose Prometheus metrics via the metrics module so the loop is visible on dashboards.
8. Defer event publishes to after-commit with `publisher.queue_for_session(...)` when the event must coincide with a durable transition (`_maybe_emit_health_changed` is the model).

## What this doc does NOT cover

- Per-axis state semantics: see Doc 1.
- Node start/stop/restart and the split-brain rules: see Doc 2.
- The HTTP shapes the loops call: see Doc 4.
- Allocator, port pools, and session reaping: see Doc 5.
