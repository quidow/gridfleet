# Device Lifecycle

## DeviceStateMachine

GridFleet centralizes composite device state transitions in
`backend/app/services/lifecycle_state_machine.py`. The machine exposes a single
`transition(device, event, *, reason=None, suppress_events=False, skip_hooks=False) -> bool`
entry point. It mutates `Device.operational_state` and `Device.hold` through
the existing `app.services.device_state` writers so
`device.operational_state_changed` and `device.hold_changed` events keep firing
on commit.

### Caller contract

- The device must be loaded under
  `app.services.device_locking.lock_device(...)` in the current transaction.
  The machine does not lock.
- Background loops and API mutators are forbidden from writing
  `Device.operational_state` or `Device.hold` directly. The two `device_state`
  writers (`set_operational_state`, `set_hold`) and the machine are the only
  sanctioned mutators; everything else routes through `transition(...)`.

### Events

| Event | Source operational state | Target | Notes |
|-------|--------------------------|--------|-------|
| `MAINTENANCE_ENTERED` | any | `(<unchanged>, maintenance)` | Hold-only mutation; operational follows from any subsequent `stop_node` cascade. Idempotent from `(*, maintenance)`. |
| `MAINTENANCE_EXITED` | `(*, maintenance)` | `(offline, None)` | Raises if hold is not maintenance. |
| `CONNECTIVITY_LOST` | `available`, `busy` | `offline` (hold preserved) | Idempotent from `offline` for any hold. |
| `CONNECTIVITY_RESTORED` | `offline` | `available` (hold preserved) | Idempotent from `available` for `None` / `reserved` hold. |
| `SESSION_STARTED` | `available`, `offline` | `busy` (hold preserved) | The `offline → busy` arc covers the production race where a Grid session arrives on a reserved-but-offline device before the connectivity loop catches up. Idempotent from `busy`. |
| `SESSION_ENDED` | `busy` | `available` (hold preserved) | Idempotent from `available` for `None` / `reserved` hold. The session_sync caller uses a fallback writer when `ready_operational_state` returns `offline` (probe failed). |
| `AUTO_STOP_EXECUTED` | `available`, `busy` | `offline` (hold preserved) | Idempotent from `offline` for any hold (handles concurrent connectivity-loop offlining). |
| `PREPARATION_FAILED` | `available`, `busy` | `offline` (hold preserved) | Modeled but no producer migrated yet — currently set by direct writers in run_service. |
| `CLOUD_ESCROW` | `available`, `busy` | `offline` (hold preserved) | Modeled; no producer migrated yet. |
| `AUTO_STOP_DEFERRED` | any | unchanged | Lifecycle JSON owns the `stop_pending` flag; transition is a no-op for the operational/hold axes. |
| `DEVICE_DISCOVERED` | any | unchanged | Initial registration is owned by ingestion code; the machine just records it. |

### Reserved-hold transparency

`hold == DeviceHold.reserved` is set by `run_reservation_service` and means a
run owns the device. The machine treats reserved as orthogonal to non-
maintenance events: connectivity, session, and auto-stop transitions all
preserve the reserved hold value.

### Maintenance-hold transparency

`hold == DeviceHold.maintenance` is set by operator-driven `enter_maintenance`. The
machine treats maintenance as orthogonal to non-maintenance events: connectivity,
session, and auto-stop transitions all preserve the maintenance hold. The
operational axis only flips to `offline` via the stop_node cascade triggered by
`enter_maintenance` when a node is running, or via `MAINTENANCE_EXITED` (which
forces operational to `offline` on exit so the recovery path can re-promote).

### Errors

Invalid transitions raise `app.errors.InvalidTransitionError`, mapped to HTTP
409 by the global exception handler with the standard `{"error": {"code": ...,
"message": ..., "details": {"event": ..., "current_state": ...}}}` envelope.

### Hooks

`DeviceStateMachine(hooks=[...])` accepts a list of `TransitionHook`
implementations. Hooks fire after the writer mutations complete, in
registration order, only when the transition actually changed state
(`changed and not skip_hooks`).

Built-in hooks:

- `EventLogHook` — records one `DeviceEvent` row per state-changing transition.
  Writes the corresponding `DeviceEventType` (e.g. `session_started`,
  `maintenance_entered`, `connectivity_lost`, `auto_stopped`) with a
  `{"from": before.label(), "to": after.label()}` detail payload. Sources the
  AsyncSession from `sa_inspect(device).session`.
- `IncidentHook`, `RunExclusionHook` — currently no-op skeletons. Lifecycle
  incidents stay at their original call sites (rich payloads are not generic
  enough to centralize), and `exclude_run_if_needed` returns `(run, entry)`
  that callers consume.

### Lifecycle JSON axis (out of scope)

`Device.lifecycle_policy_state` (the JSON column for `stop_pending`,
`backoff_until`, `recovery_suppressed_reason`, etc.) is NOT modeled by the
machine. Helpers in `app.services.lifecycle_policy_state` continue to manage
that column directly under the same row lock.
