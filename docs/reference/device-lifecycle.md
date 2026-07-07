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
`tests/test_no_direct_device_state_writes.py` enforces both rules (the call-site scan
and the attribute-assignment scan), alongside the runtime guard.

### Derived axes at a glance

| Axis | Derived from |
|------|-------------|
| `operational_state` | Session row, verification intent, appium-node stop-in-flight, device readiness, `maintenance_reason` in `lifecycle_policy_state` |
| `is_reserved` (computed) | Existence of an active `DeviceReservation` row — not a column on `Device` |

### Key rules

- **Observation loops MUST NOT write `operational_state` directly.**
  Instead they write facts and call `mark_dirty` so the reconciler re-derives state.
- **Maintenance mode** is driven by the `maintenance_reason` signal in
  `lifecycle_policy_state`.  `enter_maintenance` / `exit_maintenance` write to that
  JSON column; the reconciler derives `operational_state=maintenance` from that flag.
- **Reservation** is derived from the existence of an active `DeviceReservation` row
  and exposed as the computed `is_reserved` field on read DTOs — there is no `hold`
  column.
- **Direct attribute assignment** (`device.operational_state = ...`) is still
  **forbidden** outside the sanctioned writers.  This rule is enforced at runtime
  by the SQLAlchemy attribute-event guardrail in
  `backend/app/devices/services/state_write_guard.py`.

### Sanctioned writers

The `ALLOWLIST` dict in `state_write_guard.py` is the single source of truth for
which production modules may write each protected column.  The table below
summarizes the authoritative-state columns (operational/derived state and the
appium-node desired/transition state); for the full enumeration — including the
observation columns described further down — defer to `state_write_guard.py::ALLOWLIST`.

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
`last_health_checked_at`, `last_observed_at`.  Consult
`state_write_guard.py::ALLOWLIST` for the exact per-column writer set rather than
hand-copying it here.  Caveat: `last_observed_at` is written only by a SQLAlchemy
Core bulk update in `app.appium_nodes.services.reconciler` (`_touch_last_observed`),
which the attribute-event guard cannot intercept — Core updates never fire ORM
`set` events — so its `ALLOWLIST` entry (which names `reconciler`) is documentary
only, not enforced.

Any new sanctioned writer must be added to `ALLOWLIST`; unlisted callers get
`StateWriteOutsideSanctionedWriterError`.  Test fixtures seed state using
`state_write_guard.bypass()` — production code must never call `bypass()`.

### Row locking

Any code that writes `Device.operational_state` or `Device.lifecycle_policy_state`
MUST acquire the row lock first via `app.devices.locking.lock_device` (or
`lock_devices` for batch) inside the same transaction.  Routers should use `get_device_for_update_or_404` for state-mutating
endpoints.  Background loops commit per device after the locked write window.  The
scheduler's singleton advisory lock alone is NOT sufficient — API mutators run on every worker and
bypass it.

Exemption: `_touch_last_observed` (`app.appium_nodes.services.reconciler`) writes
`appium_nodes.last_observed_at` **lockless** by design — a monotonic observability
timestamp written by the single scheduler-serialized reconciler and read by no decision
logic, so a lost update is harmless and self-heals next tick; revisit if any decision
path ever reads it.

Alembic schema/data migrations are an accepted out-of-band writer for protected
columns; the runtime guard and row-lock contract apply to application code only.

### Lifecycle JSON axis

`Device.lifecycle_policy_state` (the JSON column for `stop_pending`,
`backoff_until`, `maintenance_reason`, `recovery_suppressed_reason`, etc.) is NOT
derived by the reconciler.  Helpers in `app.devices.services.lifecycle_policy_state`
manage that column directly under the same row lock.

### Adapter-recommended link repair

The `device_connectivity` loop dispatches manifest-declared lifecycle actions when
a health check returns a `recommended_action` (driver-agnostic: the adapter decides
whether and which action remediates; core only validates, bounds, and dispatches).
The canonical case is the android pack's `reconnect` (I10): adb transport down but
device TCP-reachable.

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
