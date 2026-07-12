# Device Lifecycle

## Writer model (Phase 2B and later)

`operational_state` is a read-time projection over durable facts. The pure evaluator
and its SQL twin live in `app/devices/services/state.py`; the five values are
`available`, `busy`, `verifying`, `offline`, and `maintenance`. Reservation is an
orthogonal `is_reserved` flag computed from `device_reservations`; there is no `hold`
column.

`Device.operational_state_last_emitted` is only the event ledger: it records the last
projected value emitted as `device.operational_state_changed`. It is not a source of
truth for current state and is written only by
`emit_operational_state_transition` in the locked intent reconciler path.

### Derivation flow

1. **Fact writers** (`device_connectivity`, `node_health`, `session_sync`,
   `session_viability`, and lifecycle services) write durable health, session,
   intent, and maintenance facts. Reads derive state immediately from those facts;
   the intent reconciler scan remains the backstop every 5 seconds.
2. **Read paths** call `derive_operational_state` / `derive_operational_states`, or
   use the SQL predicates and CASE for filtering, counts, and ordering. They:
   - Gathers facts (`DeviceStateFacts`) via DB queries (session row, verification
     intent, reservation row, maintenance flag, readiness, stop-in-flight); an
     outcome-stamped verification lease is terminal and no longer derives
     `verifying`.
   - Evaluates `evaluate_operational_state(facts)` → `DeviceOperationalState`.
   - The SQL form uses the same masking order and shared claim predicates.
3. **The edge detector** runs in the locked intent reconciler scan, compares the
   computed value with `operational_state_last_emitted`, queues one transition event
   on change, and advances the ledger. A skipped event retries on the next scan.

### When to reconcile inline (criterion)

The full scan runs every 5 seconds, but inline reconciliation is now justified only
when it changes agent-visible desired state or is required for an atomic intent
operation. Otherwise write the fact, commit, and rely on the scan:

1. **Read-your-writes** — the same flow reads the derived state or desired node
   columns after the write (e.g. `update_session_status` re-locks and acts on
   `node.stop_pending`; verification registers its lease and immediately waits
   for the node to spawn against a timeout budget, while an outcome-stamped lease
   is terminal and no longer derives `verifying`). Skipping the inline
   reconcile here is a correctness bug, not a latency cost.
2. **Intent registration/revocation** — `register_intents_and_reconcile` /
   `revoke_intents_and_reconcile` are one atomic operation under one device-row
   lock; never split the register from its reconcile.
3. **Read-your-writes for agent state** — a flow immediately consumes desired
   node fields or must make intent registration/revocation atomic. Current-state
   presentation never needs an inline reconcile.

### Derived axes at a glance

| Axis | Derived from |
|------|-------------|
| `operational_state` | Session row, unstamped verification intent, appium-node stop-in-flight, device readiness, `maintenance_reason` in `lifecycle_policy_state`; an outcome-stamped lease is terminal and does not derive `verifying` |
| `operational_state_last_emitted` | Last state transition event emitted by the reconciler edge detector |
| `is_reserved` (computed) | Existence of an active `DeviceReservation` row — not a column on `Device` |

### Reading `operational_state`

Read `operational_state` for SQL filters, counts, sorting, presentation, allocation,
and decisions that ask about the composed device state. For example, recovery code
may need to know whether the device is derived down or up after all facts have been
folded together.

For a single-axis question, read the fact for that axis instead. Use
`in_maintenance(device)` for maintenance, `device_has_live_session(...)` for a live
session, and the reservation row for reservation. The enum uses the masking order
`busy > verifying > maintenance > offline`; a higher state can hide a lower-axis
fact. A busy device can therefore still be in maintenance.

### Key rules

- **Observation loops MUST NOT write `operational_state` directly.** They write
  facts; read paths derive the projection and the reconciler owns only the event ledger.
- **Maintenance mode** is driven by the `maintenance_reason` signal in
  `lifecycle_policy_state`.  `enter_maintenance` / `exit_maintenance` write to that
  JSON column; the reconciler derives `operational_state=maintenance` from that flag.
- **Reservation** is derived from the existence of an active `DeviceReservation` row
  and exposed as the computed `is_reserved` field on read DTOs — there is no `hold`
  column.
- **Direct attribute assignment** to `operational_state_last_emitted` is forbidden
  outside the edge detector and creation seed. The static contract test scans
  production code for assignments to every protected column.

### Sanctioned writers

`PROTECTED_COLUMN_WRITERS` in
`tests/contracts/test_no_direct_device_state_writes.py` is the single source of
truth for which production modules may write each protected column. The table below
summarizes the operational and Appium-node desired/transition columns. Read the
contract test for the full per-column enumeration.

| Column | Sanctioned module |
|--------|------------------|
| `Device.operational_state_last_emitted` | `app.devices.services.state` (edge detector); `app.devices.services.write` (initial creation seed) |
| `Device.lifecycle_policy_state` | `app.devices.services.lifecycle_policy_state` |
| `AppiumNode.desired_state` / `desired_port` | `app.appium_nodes.services.desired_state_writer` |
| `AppiumNode.restart_requested_at` (restart watermark) | `app.appium_nodes.services.desired_state_writer` (no clearing protocol — a satisfied watermark is inert) |

The remaining protected columns are appium-node **observation** state, written by
the observed-state writers (the `app.appium_nodes.services.reconciler*` modules;
`app.devices.services.health` for the health fields; the active-target cache fill in
`app.devices.services.capability`; verification teardown in
`app.verification.services.execution`; and the node-creation paths for `port`):
`pid`, `port`, `active_connection_target`, `started_at` (observed Appium spawn
time, mirrored from the agent snapshot), `health_running`, `health_state`,
`last_health_checked_at`, `last_observed_at`. Consult
`PROTECTED_COLUMN_WRITERS` for the exact per-column writer set rather than copying
it into another document. Add any new sanctioned writer to that table in the same
change as the production write.

### Row locking

Any code that writes `Device.operational_state_last_emitted` or `Device.lifecycle_policy_state`
MUST acquire the row lock first via `app.devices.locking.lock_device` (or
`lock_devices` for batch) inside the same transaction.  Routers should use `get_device_for_update_or_404` for state-mutating
endpoints.  Background loops commit per device after the locked write window.  The
scheduler's singleton advisory lock alone is NOT sufficient — API mutators run on every worker and
bypass it.

Alembic schema/data migrations are an accepted out-of-band writer for protected
columns; the row-lock contract applies to application code only.

### Lifecycle JSON axis

`Device.lifecycle_policy_state` is NOT derived by the reconciler. Helpers in
`app.devices.services.lifecycle_policy_state` manage the one surviving JSON key
under the same row lock:

- `maintenance_reason`

The deferred-stop trio (`deferred_stop`, `deferred_stop_reason`,
`deferred_stop_since`) is no longer stored — it is derived from the remediation
log (`auto_stop_deferred` arms it, superseded by `auto_stopped` /
`auto_stop_cleared` / any `reset`) and gated on the live-session fact at the
projection, so a stale latch cannot form (WS-15.2).

Remediation attempts and their failure/action trail live in the append-only
`device_remediation_log` table; the "Recovery Paused" badge is projected at read time.

### Adapter-recommended link repair

The `host_sweep` connectivity stage dispatches manifest-declared lifecycle actions when
a health check returns a `recommended_action` (driver-agnostic: the adapter decides
whether and which action remediates; core only validates, bounds, and dispatches).
The canonical case is the android pack's `reconnect` (I10): adb transport down but
device TCP-reachable.

### Shared remediation escalation ladder

All automated remediations — recovery probes, node-health restarts, appium start
retries — escalate through one ladder derived by
`app/lifecycle/services/remediation_log.py` from the append-only
`device_remediation_log` table. Rows have one of four kinds: `attempt` arms the
immutable exponential backoff window, `failure` records detection context without
arming backoff, `action` stamps the action trail, and `reset` supersedes all earlier
rows for that device without erasing them.

Three reserved `action` labels also carry a **node-process directive** — the
derived successor to the retired system pseudo-commands (WS-15.2).
`auto_stop_commissioned` commissions a STOP; `restart_commissioned` and
`recovery_started` commission a START. `derive_ladder` folds the newest directive
row in the post-reset window into a `NodeDirective`, and the node-process ladder
reads it as a fact: a STOP holds the node stopped until a reset supersedes it, a
START (gated on `in_service`) outranks it structurally. A `restart_commissioned`
row's own timestamp is the restart watermark ("the Appium process must have been
spawned at or after T"); a satisfied watermark is inert, so no TTL is needed. The
ladder is promoted to
`Device.review_required` at `general.lifecycle_recovery_review_threshold`; an active
backoff window defers every automated remediation, not just the one that armed it.
Detection debounce (ip_ping duration windows, `general.node_fail_window_sec`,
probe-unanswered duration windows, and the link-repair attempt budget) stays
per-observer and only decides when a failure event is real; the ladder owns what
happens after.

Reset sources are `operator` for an explicit operator start (`operator_started`) or
restart (`operator_restarted`), `verification` for a passed re-qualification
(`verification_passed`), `device_checks` for a healthy self-heal, `recovery` when a
recovery pass finds the node already healthy (`already_healthy`), `appium_reconciler`
for a successful node start when the active episode came from that reconciler, and
run escalation when entering maintenance. A reset supersedes any live directive or
pending deferral in the episode. The operator-stop gate remains sticky, and a reset
never clears
`Device.review_required`, which is operator-owned. Callers outside the lifecycle
`write_state` allowlist escalate via
`app.lifecycle.services.actions.escalate_device_remediation_failure`.

### Recovery availability projection

"Why can automated recovery act on this device right now" is one recomputable
projection, not a stored flag. `app.devices.services.recovery_projection.recovery_availability`
folds, in order: `review_required` > recovery-deny (operator / maintenance /
cooldown, via `decide_recovery`) > not-ready > `deferred_stop` > live session >
active backoff window, returning `(allowed, reason, kind)`. Both the write path
(`attempt_auto_recovery` stands down when it reports blocked) and every read path
consult the same ladder:

- `lifecycle_policy_summary.build_lifecycle_policy` derives `recovery_state`
  (`idle | eligible | suppressed | backoff | waiting_for_session_end`) and the
  `recovery_suppressed_reason` API key **per read** — nothing is stored. The
  "Recovery Paused" badge (`kind ∈ SUPPRESSED_KINDS` = review/operator/maintenance/
  cooldown/session) appears and clears the instant the underlying fact does.
- Node `effective_state` is `blocked` when `review_required` OR an active backoff
  window is present (stored suppression is no longer consulted).

Because the projection is recomputed, there is no stale-badge class: no GC helpers,
no age-gate, no residue after self-heal or session end. Periodic recovery gating no
longer emits `lifecycle_recovery_suppressed` incidents — the causes
(maintenance_entered, operator stop, cooldown, review promotion) keep their own
events.

**Orphan systemPort cure.** When the control plane reports no live session or
in-flight probe for a device, the android adapter connect-tests the node's claimed
`appium:systemPort` (`claimed_ports_free` health check, debounced). A bound socket
with nothing live is the orphan adb-server binding (the forward table reads empty —
only a connect test sees it); the adapter recommends `release_forwarded_ports`, an
instrumented cure ladder (`forward --remove` → rebind+remove → adb bounce, the
bounce gated on no live sessions anywhere on the host). The curing rung is recorded
in the `repair_attempted` device event `detail`. Repair shares the standard attempt
budget (3, reset on healthy probe); an uncured device escalates to lifecycle policy
instead of silently failing every create.
