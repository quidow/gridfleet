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
which production modules may write each protected column.

| Column | Sanctioned module |
|--------|------------------|
| `Device.operational_state` | `app.devices.services.state` (called by `apply_derived_state`); `app.devices.services.write` (initial device creation only) |
| `Device.lifecycle_policy_state` | `app.devices.services.lifecycle_policy_state` |
| `AppiumNode.desired_state` / `desired_port` / `transition_token` / `transition_deadline` | `app.appium_nodes.services.desired_state_writer` |

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
derived by the reconciler.  Helpers in `app.services.lifecycle_policy_state`
manage that column directly under the same row lock.
