# Device Lifecycle

## Writer model (Phase 2B and later)

`Device.operational_state` is **derived** by the `device_intent_reconciler` loop
(`app/devices/services/intent_reconciler.py`) — the authoritative writer for every
observation-driven transition and for the normal verification pass / update-failure
terminals.  `operational_state` is a 5-value enum: `available`, `busy`, `verifying`,
`offline`, `maintenance`.  Reservation is an orthogonal concern surfaced as the
`is_reserved` flag computed from the `device_reservations` table; there is no `hold`
column.

### Derivation flow

1. **Observation loops** (`device_connectivity`, `node_health`, `session_sync`,
   `session_viability`) write durable facts — health flags, session rows,
   `maintenance_reason` in `lifecycle_policy_state` — then call
   `IntentService.mark_dirty_and_reconcile` (or `mark_dirty`) to signal that the
   reconciler should re-derive state on its next tick.
2. **Reconciler tick** calls `apply_derived_state` in
   `app/devices/services/state.py`, which:
   - Gathers facts (`DeviceStateFacts`) via DB queries (session row, verification
     intent, reservation row, maintenance flag, readiness, stop-in-flight).
   - Evaluates `evaluate_operational_state(facts)` → `DeviceOperationalState`.
   - Writes the new value through `set_operational_state`
     (`app.devices.services.state`) only when the derived value differs from the
     persisted column.
   - Emits the mapped event if the value actually changed.

`apply_derived_state` (invoked by `reconcile_device` / `IntentService.*_and_reconcile`)
is the **only** runtime writer of `operational_state`. There are no direct
(non-reconciler) callers of `set_operational_state` left: observation and lifecycle
sites — host-offline cascade, run release, session registration, verification entry —
write durable facts (health flags, session rows, `maintenance_reason`, verification-lease
intents) and trigger an inline reconcile via `IntentService.mark_dirty_and_reconcile` /
`register_intents_and_reconcile`, so the state is still written synchronously before
commit, just via derivation. Device-creation paths still set the initial value at
construction time (`app.devices.services.write`). The static test
`tests/contracts/test_no_direct_device_state_writes.py` enforces both rules with a
call-site scan and a table-driven attribute-assignment scan.

### Derived axes at a glance

| Axis | Derived from |
|------|-------------|
| `operational_state` | Session row, verification intent, appium-node stop-in-flight, device readiness, `maintenance_reason` in `lifecycle_policy_state` |
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

- **Observation loops MUST NOT write `operational_state` directly.**
  Instead they write facts and call `mark_dirty` so the reconciler re-derives state.
- **Maintenance mode** is driven by the `maintenance_reason` signal in
  `lifecycle_policy_state`.  `enter_maintenance` / `exit_maintenance` write to that
  JSON column; the reconciler derives `operational_state=maintenance` from that flag.
- **Reservation** is derived from the existence of an active `DeviceReservation` row
  and exposed as the computed `is_reserved` field on read DTOs — there is no `hold`
  column.
- **Direct attribute assignment** (`device.operational_state = ...`) is forbidden
  outside the sanctioned writers. The static contract test scans production code
  for assignments to every protected column.

### Sanctioned writers

`PROTECTED_COLUMN_WRITERS` in
`tests/contracts/test_no_direct_device_state_writes.py` is the single source of
truth for which production modules may write each protected column. The table below
summarizes the operational and Appium-node desired/transition columns. Read the
contract test for the full per-column enumeration.

| Column | Sanctioned module |
|--------|------------------|
| `Device.operational_state` | `app.devices.services.state` (called by `apply_derived_state`); `app.devices.services.write` (initial device creation only) |
| `Device.lifecycle_policy_state` | `app.devices.services.lifecycle_policy_state` |
| `AppiumNode.desired_state` / `desired_port` | `app.appium_nodes.services.desired_state_writer` |
| `AppiumNode.transition_token` / `transition_deadline` | `app.appium_nodes.services.desired_state_writer`, plus sanctioned direct clears by `app.appium_nodes.services.reconciler_agent` and `app.appium_nodes.routers.admin` (the latter under the device row lock) |

The remaining protected columns are appium-node **observation** state, written by
the observed-state writers (the `app.appium_nodes.services.reconciler*` modules;
`app.devices.services.health` for the health fields; the active-target cache fill in
`app.devices.services.capability`; verification teardown in
`app.verification.services.execution`; and the node-creation paths for `port`):
`pid`, `port`, `active_connection_target`, `health_running`, `health_state`,
`last_health_checked_at`, `last_observed_at`. Consult
`PROTECTED_COLUMN_WRITERS` for the exact per-column writer set rather than copying
it into another document. Add any new sanctioned writer to that table in the same
change as the production write.

### Row locking

Any code that writes `Device.operational_state` or `Device.lifecycle_policy_state`
MUST acquire the row lock first via `app.devices.locking.lock_device` (or
`lock_devices` for batch) inside the same transaction.  Routers should use `get_device_for_update_or_404` for state-mutating
endpoints.  Background loops commit per device after the locked write window.  The
scheduler's singleton advisory lock alone is NOT sufficient — API mutators run on every worker and
bypass it.

Alembic schema/data migrations are an accepted out-of-band writer for protected
columns; the row-lock contract applies to application code only.

### Lifecycle JSON axis

`Device.lifecycle_policy_state` (the JSON column for `stop_pending`,
`backoff_until`, `maintenance_reason`, `recovery_suppressed_reason`, etc.) is NOT
derived by the reconciler.  Helpers in `app.devices.services.lifecycle_policy_state`
manage that column directly under the same row lock.

### Adapter-recommended link repair

The `host_sweep` connectivity stage dispatches manifest-declared lifecycle actions when
a health check returns a `recommended_action` (driver-agnostic: the adapter decides
whether and which action remediates; core only validates, bounds, and dispatches).
The canonical case is the android pack's `reconnect` (I10): adb transport down but
device TCP-reachable.

### Shared remediation escalation ladder

All automated remediations — recovery probes, node-health restarts, appium start
retries — escalate through one ladder owned by
`app/lifecycle/services/escalation.py`: `recovery_backoff_attempts` and
`backoff_until` on `lifecycle_policy_state`, exponential backoff from
`general.lifecycle_recovery_backoff_base_sec` capped at
`general.lifecycle_recovery_backoff_max_sec`, promoted to
`Device.review_required` at `general.lifecycle_recovery_review_threshold`. An armed
backoff window defers every automated remediation, not just the one that armed it.
Detection debounce (ip_ping hysteresis, `general.node_max_failures`,
probe-unanswered counting, the link-repair attempt budget) stays per-observer and
only decides when a failure event is real; the ladder owns what happens after.

A successful node start clears only reconciler-sourced residue
(`last_failure_source == "appium_reconciler"`). Backoff from failed recovery probes
or node-health restarts survives until a verified recovery — a probe pass or the
connectivity self-heal — clears it. Callers outside the lifecycle `write_state`
allowlist escalate via
`app.lifecycle.services.actions.escalate_device_remediation_failure`.

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
