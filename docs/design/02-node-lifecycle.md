# Doc 2: Node Lifecycle

> Implementation contract for starting, stopping, restarting, and recovering an Appium node — the split-brain rules that keep the DB row and the host process consistent.

The Appium node is the most failure-prone object in GridFleet. It lives in two places at once (a row in `appium_nodes` on the manager and a real Appium subprocess on the host) and a session is served only when both halves agree. Most node-related bugs are split-brain bugs: one half flipped state without the other.

## Cast of characters

| Component | Role |
| --- | --- |
| `reconciler_agent` (`backend/app/appium_nodes/services/reconciler_agent.py`) | `mark_node_started`/`mark_node_stopped` (observed-state writers) plus `ReconcilerAgentService` (`start_node`/`stop_node`/`restart_node`/`wait_for_node_running`) — these register desired-state intents only; none call the agent |
| Observe-only convergence (`backend/app/appium_nodes/services/reconciler.py`) | Per-host: matches the agent's latest pushed status report against desired rows (`decide_convergence_action`), writes DB-only observed facts, ingests start failures; issues no start/stop/restart |
| `host_sweep` node-health fold (`backend/app/appium_nodes/services/node_health.py`) | Folds the pushed node-health section; at the failure window escalates through the shared remediation ladder — never calls the agent |
| `poke_node_refresh` (`backend/app/agent_comm/node_poke.py`) | Fire-and-forget wake hint after every desired-state write — the only backend→agent node signal |
| Host agent `NodeStateLoop` (`agent/agent_app/appium/node_state.py`) | Pulls `GET /agent/appium-nodes/desired`, diffs against local Appium processes, owns start/stop/reconfigure/orphan-reap for its host |

## The DB↔agent contract in one sentence

> **The DB row only flips observed state on the agent's own pushed report, and the agent only owns process state.**

The five rules:

1. The backend never starts or stops an Appium process. It writes desired state (row-locked, via `write_desired_state`) and sends a best-effort poke; the agent's `NodeStateLoop` pulls and applies locally.
2. Observed-state writes are gated on the agent's pushed self-report (`POST /agent/hosts/status`), never assumed from the absence of an error.
3. `mark_node_started` / `mark_node_stopped` fire only from the observe-only convergence match — never speculatively. A missing or stale report changes nothing; the DB stays put until a later sweep sees a confirming report.
4. `DeviceHealthService.apply_node_state_transition` is the node-health writer inside that transaction, so health detail and `device.health_changed` land with the flip.
5. Resource claims (ports + per-host capabilities) are keyed by `node_id` and are never released on stop: they live for the lifetime of the `AppiumNode` row (cascade-deleted with it), so a stopped node's ports can never be handed to a different node.

## Node state machine

No `state` column exists. Two orthogonal axes:

- **Desired:** `AppiumNode.desired_state` (`running` | `stopped`, DB CHECK constraint — there is no `error` desired state), `desired_port`, and the `restart_requested_at` watermark, all written only through `write_desired_state`.
- **Observed:** the computed `observed_running` property (`pid` AND `active_connection_target` both set), written by `mark_node_started` / `mark_node_stopped`. Health failure is tracked on the node (`health_failing_since` / `health_running` / `health_state`), but the failure terminal is on the *device* (projection `offline`), not a node state.

Transitions, all pull-driven:

- **stopped → running:** desired write + poke → agent pulls and starts locally → the next push lists the process → convergence runs `mark_node_started`.
- **running → stopped:** mirror image; `mark_node_stopped` fires only once the push shows the process absent. Until then `pid`/`active_connection_target` stay set — an unconfirmed orphan may still be serving traffic on its port, and the DB must keep saying so.
- **Restart:** no desired flip. `write_desired_state` stamps `restart_requested_at` ("the process must have been spawned at or after T"); the agent respawns to satisfy it; the backend confirms by comparing the watermark against the push-reported `started_at`. No clearing protocol exists — a satisfied watermark is inert, and a stuck "restarting" projection self-clears at read time after `appium_reconciler.restart_window_sec` (Doc 4).

Operator action writes the **desired** axis only. Health failure stops nothing by itself — it advances `health_failing_since`, and at `general.node_fail_window_sec` escalates through the remediation ladder.

## The four flows, compressed

- **Operator start** (`ReconcilerAgentService.start_node`): readiness gate (`is_ready_for_use_async`), then registers the operator start intent (`operator_node.request_start` → `register_intents_and_reconcile`), which writes `desired_state=running` with a candidate `desired_port`, pokes, and returns — the process is not yet up. Callers that must block use `wait_for_node_running`, which polls `observed_running`. Port selection happens at desired-write time via `candidate_ports`; there is no agent-side port allocation call.
- **Operator stop** (`ReconcilerAgentService.stop_node`): registers the operator stop intent, writes `desired_state=stopped`, pokes, returns. The observed flip waits for a push that no longer lists the process.
- **Operator restart** (`ReconcilerAgentService.restart_node`): short-circuits to `start_node` when not `observed_running`; otherwise stamps the restart watermark and pokes.
- **Auto-recovery:** the node-health fold detects the failure window and escalates via the shared remediation ladder (`escalate_device_remediation_failure` → `device_remediation_log`); the ladder's directive commissions the node-process action and its row timestamp is the restart watermark, so the do-once semantics need no extra state. Backoff, exhaustion, and review promotion all derive from the log — see `docs/reference/device-lifecycle.md`, "Shared remediation escalation ladder".

Failure modes on the start path: an agent that has not pulled yet means no progress and no inconsistency (`desired_state=running` persists; the agent converges on its next successful pull). A local start failure is reported on the next push as `start_failures` and handled per the next section.

## Port-conflict semantics

Port conflict is detected by the agent, locally, at spawn time. A raised local port-conflict error is reported as `start_failures: [{kind: "port_conflict", connection_target, port, detail, at}]` on the next consolidated status push; any other start exception reports `kind: "spawn_failed"`. The convergence pass ingests the report:

| Kind | Backend reaction |
| --- | --- |
| `port_conflict` | Records the start-failure backoff (`_record_start_failure`) **and** re-pins `desired_port` to the next free candidate (`_repin_desired_port` via `candidate_ports`) |
| `spawn_failed` | Records the start-failure backoff only; no re-pin |

There is no same-attempt retry loop: a conflict costs one pull/report/re-pin/pull round trip. `candidate_ports` (`reconciler_allocation.py`) excludes ports of nodes that are observed-running **or** desired-running on that host, so a recovery rebind intentionally lands on a different free port rather than racing an unconfirmed orphan on the old one. When the pool is exhausted it raises `NodeManagerError`.

## Resource claims

Claims are keyed by `node_id`, reserved via `appium_node_resource_service`, never released on stop, and cascade-deleted with the node row — so the same node retakes its ports across stop/start and no other node can grab them while the row is alive. Doc 5 owns the details.

## Lock acquisition order (deadlock avoidance)

```text
1. device_locking.lock_device(db, device.id)
2. appium_node_locking.lock_appium_node_for_device(db, device.id)
3. writers: observed (pid / active_connection_target via mark_node_started/mark_node_stopped);
   desired (desired_state / desired_port / restart_requested_at via write_desired_state);
   Device.operational_state_last_emitted / Device.lifecycle_policy_state
4. DeviceHealthService(...).apply_node_state_transition(...)
5. publisher.queue_for_session(...)
6. db.commit()
```

Event publishes are deferred to after-commit by `queue_for_session`: subscribers must never observe a transition that did not become durable.

## Split-brain prevention checklist

For every new code path that touches node state, verify:

- [ ] Observed-state writes are gated on the agent's pushed self-report, never assumed.
- [ ] `mark_node_started` / `mark_node_stopped` fire only from the convergence match, never speculatively.
- [ ] They run inside a transaction holding the device row lock (then the node lock).
- [ ] `DeviceHealthService.apply_node_state_transition` is the node-health writer in that transaction.
- [ ] Resource claims are released only on node/device deletion, never on stop.
- [ ] On an agent-reported `port_conflict`, `desired_port` is re-pinned; the agent converges on its next pull.
- [ ] After any `mark_node_*`, the `node.state_changed` publish is queued before commit.

The next class of bugs will come from a new code path that skipped one of these. This checklist is the trip-wire.

## Known vestiges

`NodePortConflictError` (`app/appium_nodes/exceptions.py`) is still caught defensively (`routers/nodes.py`, `devices/routers/control.py`) but raised nowhere in the pull-model start/stop path; `RESTART_BACKOFF_BASE` / `RESTART_MAX_RETRIES` in `reconciler_agent.py` are dead. Both are WS-17.2 deletion targets — do not build on them.

## What this doc does NOT cover

- Per-axis details of `Device` state: see Doc 1.
- Loop cadences and the tri-state probe: see Doc 3.
- HTTP request/response shapes: see Doc 4.
- Allocator implementation, port-pool seeding, session reaping: see Doc 5.
