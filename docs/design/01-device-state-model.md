# Doc 1: Device State Model

> Implementation-level reference. Operator-facing semantics: `docs/guides/lifecycle-maintenance-and-recovery.md` and `docs/guides/verification-and-readiness.md`. The `operational_state` writer model is canonically specified in `docs/reference/device-lifecycle.md`; this doc summarizes it and must not contradict it.

A `Device` row carries **multiple independent axes** of state. They look related on the UI, but they are written by different code paths, gated by different rules, and recover at different speeds. Treating them as one knob is the root cause of most "split-brain" bugs. This doc is the contract for those axes: what each means, where it lives, and who may write or derive it.

## TL;DR

| Axis | Source of truth | Who writes / derives |
| --- | --- | --- |
| Readiness | derived, not stored | `app.devices.services.readiness.is_ready_for_use_async` |
| Operational state | read-time projection over durable facts | evaluator + SQL twin in `app/devices/services/state.py`; nobody stores current state |
| Operational-state event ledger | `Device.operational_state_last_emitted` | `emit_operational_state_transition` in the locked intent-reconciler edge detector, plus the device-creation seed |
| Reservation | `device_reservations` rows | computed `is_reserved` via `app.devices.services.reservation_query.device_is_reserved` |
| Hardware health | `Device.hardware_health_status` | the `host_sweep` hardware-telemetry fold |
| Lifecycle JSON | `Device.lifecycle_policy_state` | `app.devices.services.lifecycle_policy_state` helpers, under the row lock |
| Remediation memory | `device_remediation_log` (append-only) | ladder writers via `app.lifecycle.services.actions.escalate_device_remediation_failure`; derivations in `app/lifecycle/services/remediation_log.py` |
| Node state | `AppiumNode.desired_state` + observed columns | desired via `write_desired_state`; observed per `PROTECTED_COLUMN_WRITERS` |
| Health | typed columns on `Device` and `AppiumNode` | fact writers per Doc 3; public summary derived on read |

## Axis: Readiness

Readiness answers "is the saved configuration safe to start a node against?". It is derived, not stored: inputs are `Device.verified_at`, `Device.device_config`, `Device.ip_address`, and `Device.connection_type`, computed by `is_ready_for_use_async` and surfaced as the readiness badge. It is the first gate on every state-changing API call — the start-node path refuses when it fails. Only operator-driven flows change it; no background loop flips it.

## Axis: Operational state (a projection, not a column to write)

```text
operational_state : available | busy | verifying | offline | maintenance
```

`operational_state` is a **read-time projection from durable facts** — there is no stored current-state value to update, and therefore nothing to forget or clear. The pure evaluator (`evaluate_operational_state`), the fact gather (`gather_device_state_facts`), and the SQL twin (`operational_state_sql` plus the shared claim predicates) live in `app/devices/services/state.py`. Facts folded in: an active client session, an unstamped verification lease, `maintenance_reason`, node stop-in-flight, readiness. Probe sessions claim the device but are excluded from the `busy` masking input (one explicit rule in the shared claim-predicate module).

Masking order is `busy > verifying > maintenance > offline`: a higher state can hide a lower-axis fact, so ask single-axis questions of the fact itself — `in_maintenance(device)`, `device_has_live_session(...)`, or the reservation row. Use `operational_state` for SQL filters, counts, presentation, allocation, and composed-state gates.

`Device.operational_state_last_emitted` is only the **event ledger**: the last projected value emitted as `device.operational_state_changed`. It is written only by `emit_operational_state_transition` inside the locked intent-reconciler edge detector, plus the creation seed. Every other code path writes durable facts. The contract test `tests/contracts/test_no_direct_device_state_writes.py` enforces this; `docs/reference/device-lifecycle.md` is the canonical spec (derivation flow, inline-reconcile criterion, the priced `_ready_sql` approximation).

### Reservation (computed, not a column)

Active reservations are rows in `device_reservations`; the `is_reserved` flag is computed via `device_is_reserved` and surfaced through the presenter. A device can be reserved while the projection reads `offline` (the agent died but the run keeps the device). Reservation is never part of the status chip; it is an orthogonal boolean filter.

## Axis: Hardware health (`Device.hardware_health_status`)

`unknown · healthy · warning · critical`, written exclusively by the `host_sweep` hardware-telemetry fold from pushed battery/temperature reports. Never read by node-lifecycle code; operator dashboard only. Live surface: `device.hardware_health_changed` (telemetry transitions), `device.health_changed` (aggregate summary transitions), `device.crashed` (per-device crash incidents; distinct from the per-process `node.crash`).

## Axis: Lifecycle JSON + remediation memory

`Device.lifecycle_policy_state` holds **one** surviving key: `maintenance_reason`. It is a read-modify-write JSON field — any writer uses the `app.devices.services.lifecycle_policy_state` helpers while holding the device row lock for the whole RMW window; direct assignment in production code is a bug.

The auto-recovery machine's memory — attempts, backoff, directives, failure/action trail, deferred stops — is **not** stored here. It is the append-only `device_remediation_log`, derived at read time by `app/lifecycle/services/remediation_log.py` (supersession replaces erasure; there are no clearers). "Why is recovery blocked right now" is likewise the recomputed `recovery_availability` projection, never a stored flag. See `docs/reference/device-lifecycle.md` — "Shared remediation escalation ladder" and "Recovery availability projection".

## Axis: Node state (desired vs observed)

There is no `AppiumNode.state` column. The row carries a 2-value **desired** axis (`desired_state : running | stopped`, plus `desired_port` and the `restart_requested_at` watermark), written only through `write_desired_state` under the device row lock, and separate **observed** columns (`pid`, `port`, `active_connection_target`, `started_at`, health fields) written only from the agent's pushed self-report. The effective state is derived. The node is a separate row (one-to-one with `Device`, FK cascade): a device exists without a node, never the reverse.

Per-column sanctioned writers are enumerated **only** in `PROTECTED_COLUMN_WRITERS` (`tests/contracts/test_no_direct_device_state_writes.py`) — consult it there; do not copy it into documents. Add any new sanctioned writer to that table in the same change as the production write. Doc 2 covers the transitions and the split-brain rules.

## Axis: Health (derived on read)

Health-relevant facts live in typed columns: `Device.device_checks_*`, `Device.session_viability_*`, `Device.emulator_state`, and the node's health fields (`health_running`, `health_state`, `health_failing_since`, `last_health_checked_at`). The public summary returned by `/api/devices` is computed on read by `build_public_summary(device)` (`app.devices.services.health_view`, re-exported via `app.devices.services.health`) — a pure function; there is no stored health document to drift.

Rules the writers obey:

1. **Typed values only.** `update_device_checks` / `update_session_viability` take real booleans; indeterminate probe results short-circuit before the call (`ProbeResult`, Doc 3).
2. **The cross-link to operational state is read-time.** Observation writers record durable health facts; the projection folds them the moment they commit, and the reconciler's edge detector emits the transition event. Observation writers never touch the projection or its ledger.
3. **No public health KV.** `control_plane_state_store` holds only ephemeral loop coordination (Doc 3's namespace table), never canonical health.

## The locking invariant

```text
Any write to Device.operational_state_last_emitted or Device.lifecycle_policy_state
MUST hold a row-level lock in the same transaction as the write.
```

Use `app.devices.locking` (`lock_device` / `lock_devices`; batches lock ids ascending so single-row and batch callers stay deadlock-free). Routers use `get_device_for_update_or_404` for state-mutating endpoints. The run allocator is the exception by design: `_find_matching_devices` (`app.runs.service_allocator`) locks allocatable rows with `SELECT ... FOR UPDATE SKIP LOCKED` before reserving. The scheduler's advisory lock is NOT sufficient — API mutators run on every worker and bypass it.

`AppiumNode` desired-state writes also hold `lock_appium_node_for_device`, acquired after the device lock.

## What this doc does NOT cover

- The node state machine and the agent-report contract: see Doc 2.
- Loop cadences and the tri-state probe: see Doc 3.
- HTTP shapes between backend and agent: see Doc 4.
- Allocation, port pools, and sessions: see Doc 5.
