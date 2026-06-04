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
   `app/devices/services/state_derivation.py`, which:
   - Gathers facts (`DeviceStateFacts`) via DB queries (session row, verification
     intent, reservation row, maintenance flag, readiness, stop-in-flight).
   - Evaluates `evaluate_operational_state(facts)` → `DeviceOperationalState`.
   - Writes the new value through `set_operational_state`
     (`app.devices.services.state`) only when the derived value differs from the
     persisted column.
   - Emits the mapped event if the value actually changed.

`apply_derived_state` has a single production caller: the `device_intent_reconciler`
loop.  Besides that derived path, `set_operational_state` has **four direct
(non-reconciler) production callers** for specific entry/exit states:

- `app.verification.services.execution` — transient `verifying` entry state.
- `app.sessions.service` (`register_session`) — sets `busy` on session registration.
- `app.appium_nodes.services.heartbeat` — host-offline path that marks every device
  on a downed host `offline`.
- `app.runs.service_lifecycle_release` (`release_devices`) — restores the ready
  operational state when a run ends.

The verification pass / update-failure terminals are reconciler-derived, not direct
writes.

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
`set` events — so the `ALLOWLIST` entry naming `heartbeat` for that column is stale
and unenforced.

Any new sanctioned writer must be added to `ALLOWLIST`; unlisted callers get
`StateWriteOutsideSanctionedWriterError`.  Test fixtures seed state using
`state_write_guard.bypass()` — production code must never call `bypass()`.

### Row locking

Any code that writes `Device.operational_state` or `Device.lifecycle_policy_state`
MUST acquire the row lock first via `app.devices.locking.lock_device` (or
`lock_devices` for batch) inside the same transaction.  Routers should use `get_device_for_update_or_404` for state-mutating
endpoints.  Background loops commit per device after the locked write window.  The
leader advisory lock alone is NOT sufficient — API mutators run on every worker and
bypass it.

### Lifecycle JSON axis

`Device.lifecycle_policy_state` (the JSON column for `stop_pending`,
`backoff_until`, `maintenance_reason`, `recovery_suppressed_reason`, etc.) is NOT
derived by the reconciler.  Helpers in `app.devices.services.lifecycle_policy_state`
manage that column directly under the same row lock.
