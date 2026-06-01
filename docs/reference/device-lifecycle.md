# Device Lifecycle

## Writer model (Phase 2B and later)

`Device.operational_state` and `Device.hold` are **derived** by a single authoritative
writer: the `device_intent_reconciler` loop
(`app/devices/services/intent_reconciler.py`).

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
   - Evaluates `evaluate_hold(facts)` → `DeviceHold | None`.
   - Writes the new values through `set_operational_state` / `set_hold`
     (`app.devices.services.state`) only when the derived value differs from the
     persisted column.
   - Emits the mapped event for each axis that actually changed.

### Derived axes at a glance

| Column | Derived from |
|--------|-------------|
| `operational_state` | Session row, verification intent, appium-node stop-in-flight, device readiness |
| `hold` | `maintenance_reason` in `lifecycle_policy_state` (maintenance) or active `DeviceReservation` row (reserved) |

### Key rules

- **Observation loops MUST NOT call `DeviceStateMachine.transition` directly.**
  Direct `_MACHINE.transition` calls from observation loops have been removed.
  Instead they write facts and call `mark_dirty`.
- **Maintenance mode** is driven by the `maintenance_reason` signal in
  `lifecycle_policy_state`.  `enter_maintenance` / `exit_maintenance` write to that
  JSON column; the reconciler observes the flag when deriving `hold=maintenance`.
- **`reserved` hold** is derived from the existence of an active `DeviceReservation`
  row, not from a direct `hold` write.
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
| `Device.hold` | `app.devices.services.state` (called by `apply_derived_state`) |
| `Device.lifecycle_policy_state` | `app.devices.services.lifecycle_policy_state` |
| `AppiumNode.desired_state` / `desired_port` / `transition_token` / `transition_deadline` | `app.appium_nodes.services.desired_state_writer` |

Any new sanctioned writer must be added to `ALLOWLIST`; unlisted callers get
`StateWriteOutsideSanctionedWriterError`.  Test fixtures seed state using
`state_write_guard.bypass()` — production code must never call `bypass()`.

### Row locking

Any code that writes `Device.operational_state`, `Device.hold`, or
`Device.lifecycle_policy_state` MUST acquire the row lock first via
`app.devices.locking.lock_device` (or `lock_devices` for batch) inside the same
transaction.  Routers should use `get_device_for_update_or_404` for state-mutating
endpoints.  Background loops commit per device after the locked write window.  The
leader advisory lock alone is NOT sufficient — API mutators run on every worker and
bypass it.

## Legacy: DeviceStateMachine

`DeviceStateMachine` (`app/devices/services/lifecycle_state_machine.py`) still exists
but its role is reduced.  The machine's `transition(device, event, ...)` entry point
is no longer called by observation loops.  The machine remains active only where it is
still directly wired — see the `state_write_guard.py` ALLOWLIST for the current
authoritative list of callers.

### Still-active machine events

| Event | Notes |
|-------|-------|
| `VERIFICATION_STARTED` / `VERIFICATION_PASSED` / `VERIFICATION_FAILED` | Called from `app/devices/services/verification_execution.py` (4 call sites). |

### Removed machine events (Phase 2B)

The following events no longer have active producers:

| Event | Removed from |
|-------|-------------|
| `CONNECTIVITY_LOST` | Connectivity loop now calls `mark_dirty_and_reconcile` instead |
| `CONNECTIVITY_RESTORED` | Connectivity loop uses `attempt_auto_recovery` → `mark_dirty` |
| `SESSION_STARTED` / `SESSION_ENDED` | `session_sync` now calls `mark_dirty` |
| `AUTO_STOP_EXECUTED` | Lifecycle policy loop calls `mark_dirty` |
| `MAINTENANCE_ENTERED` / `MAINTENANCE_EXITED` | Maintenance state is now driven by `maintenance_reason` in `lifecycle_policy_state` and derived by the reconciler; the machine is no longer called. |

### Hooks

`DeviceStateMachine(hooks=[...])` still accepts `TransitionHook` implementations.

Built-in hooks:

- `EventLogHook` — records one `DeviceEvent` row per state-changing transition.
- `IncidentHook`, `RunExclusionHook` — currently no-op skeletons.

### Lifecycle JSON axis

`Device.lifecycle_policy_state` (the JSON column for `stop_pending`,
`backoff_until`, `maintenance_reason`, `recovery_suppressed_reason`, etc.) is NOT
managed by the state machine.  Helpers in `app.services.lifecycle_policy_state`
manage that column directly under the same row lock.
