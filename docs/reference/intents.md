# Device Intents

The control plane drives every device's desired state through the
`device_intents` table. Each row is one "this source wants axis X in state Y at
priority P." The reconciler evaluates all active intents per device every tick
and writes the derived state to the device row and its Appium node.

## Lifecycle

An intent is alive between registration and deletion. There are three mechanisms
that delete intents:

1. Explicit revoke via `revoke_intents_and_reconcile(...)` from the site that
   drives the underlying state change.
2. Time TTL via `expires_at`, swept once per reconciler tick by
   `_reconcile_expired_intents`.
3. Precondition via the optional `precondition` JSONB column, swept once per
   reconciler tick by `reconcile_unsatisfied_preconditions`.

A precondition is the preferred mechanism for any cleanup condition that can be
expressed as "this intent dies when entity X reaches state Y" because the
condition lives at the registration site, not scattered across every revoke
call site.

## Supported Predicate Kinds

Each predicate is a structured dict stored in the `precondition` column.

### `run_active`

```json
{"kind": "run_active", "run_id": "<uuid>"}
```

Satisfied while the referenced run's state is not in `TERMINAL_STATES`. Used for
run-scoped intents such as `cooldown:*` and `forced_release:*`.

### `reservation_active`

```json
{"kind": "reservation_active", "run_id": "<uuid>", "device_id": "<uuid>"}
```

Satisfied while a `DeviceReservation(run_id, device_id)` row exists with
`released_at IS NULL`. Used for the `run:<run>` grid-routing intent.

### `node_running`

```json
{"kind": "node_running", "device_id": "<uuid>", "expected": false}
```

Satisfied while `AppiumNode.observed_running == expected`. Used for
`auto_recovery:*` intents that intend to restart a stopped node and must retire
once the node is running.

### `maintenance_active`

```json
{"kind": "maintenance_active", "device_id": "<uuid>"}
```

Satisfied while the device's `lifecycle_policy_state` `maintenance_reason` is not
None (evaluated via `_eval_maintenance_active`). Used for `maintenance:*` intents
that should clear when the operator exits maintenance.

## Adding A New Predicate

1. Add a `TypedDict` to `app/devices/services/intent_types.py` and extend the
   `Precondition` union.
2. Add a `_eval_<kind>` helper in
   `app/devices/services/intent_evaluator.py` and a branch in
   `is_satisfied`.
3. Add per-kind unit tests in `tests/test_intent_evaluator_preconditions.py`.
4. Set the precondition at every registration site that should use it.
5. Update this catalog.

## Backwards Compatibility

A row with `precondition IS NULL` behaves exactly as before this change. Only
the explicit revoke and `expires_at` mechanisms can delete it.

Manual revoke calls remain functional. A precondition is additive: if both
mechanisms could delete the intent, whichever fires first wins.

## Out Of Scope

The priority-based evaluator in `intent_evaluator.py` is untouched. Priorities
still decide which active intent wins per axis.

Recovery suppression is handled by `Device.review_required`, not by intents. See
[device-lifecycle.md](./device-lifecycle.md).

## Payload Field Policy

Each `IntentRegistration` payload field falls under one of three policies. The
policy choice determines whether the field's value at registration is
authoritative (snapshot) or must be re-evaluated on each cycle.

| Policy | When to use | Implementation |
|--------|-------------|----------------|
| Drop | Field captures moving state with a live source on the row (port, pid, count) | Remove from payload. Reconciler reads live `node.<field>` via fallback. |
| Refresh-on-event | Field needs to outlive the row (TTL, deadline, exclusion reason) | Keep; renewal trigger documented. Re-register on each trigger fire. |
| Intentional snapshot | Field captures a historical fact (cooldown reason at registration, exclusion reason for audit) | Keep. Comment: `# intentionally captured at registration: <reason>`. |

### Per-source payload table

One row per (source, field) pair across all 31 `IntentRegistration` call sites.
`priority` and axis-required fields (`action`, `accepting_new_sessions`,
`excluded`, `allowed`) are omitted — they are structural and always intentional
snapshots of the caller's intent. Only semantically interesting fields appear
below.

| Source | Field | Policy | Notes |
|--------|-------|--------|-------|
| `auto_recovery:node:{device_id}` (node_health path) | `transition_token` | Intentional snapshot | Fresh UUID minted at registration; coordinates the reconciler's desired-state write window. |
| `auto_recovery:node:{device_id}` (node_health path) | `transition_deadline` | Intentional snapshot | Window computed fresh at registration from `appium_reconciler.restart_window_sec`; not read from a live row field. |
| `auto_recovery:node:{device_id}` (lifecycle_policy path) | _(no extra fields)_ | — | Only structural fields; lifecycle_policy path omits `transition_token`/`transition_deadline`. Precondition guard matches the node_health path (`node_running`, `expected: False`) in the normal case; under a stale-offline observation (`stale_offline_observation`) the precondition is omitted and the intents are bounded by a TTL (`expires_at` = `startup_timeout_sec` + `session_viability_timeout_sec` + 60 s) instead, because the `node_running` precondition would key off the same stale observation and get reaped before the agent starts the node. See lifecycle ownership below. |
| `auto_recovery:recovery:{device_id}` | `reason` | Intentional snapshot | Captures the human-readable trigger at the moment recovery is initiated ("Node health restart" or the policy `reason`). |
| `verification:{device_id}` | `expires_at` (TTL) | Refresh-on-event | `startup_timeout_sec` + `session_viability_timeout_sec` + 60 s safety margin; re-registered each verification start (verification execution `_register_verification_node_intent`) and on `exit_maintenance`. |
| `active_session:{sid}` | _(no extra fields)_ | — | Structural only (`action`, `priority`). |
| `run:{run_id}` (grid_routing) | `accepting_new_sessions` | Refresh-on-event | Re-registered on each allocation / restore-to-run call; structural for the axis. |
| `forced_release:{run_id}` | `stop_mode` | Intentional snapshot | "hard" stop is a policy decision made at the moment of force-release; not a moving row value. |
| `cooldown:node:{run_id}` | `stop_mode` | Intentional snapshot | "defer" policy set at cooldown registration; fixed for the cooldown lifetime. |
| `cooldown:grid:{run_id}` | _(no extra fields)_ | — | Structural only. |
| `cooldown:reservation:{run_id}` | `cooldown_count` | Refresh-on-event | Re-registered on each cooldown increment; reconciler's `_update_reservation_exclusion` takes `max()` across concurrent registrations. |
| `cooldown:reservation:{run_id}` | `exclusion_reason` | Intentional snapshot | Captures the failure description at the time of the increment. |
| `cooldown:recovery:{run_id}` | `reason` | Intentional snapshot | Same failure description captured at increment time. |
| `device_delete:node:{device_id}` | `stop_mode` | Intentional snapshot | "graceful" stop policy fixed at delete time. |
| `device_delete:recovery:{device_id}` | `reason` | Intentional snapshot | "Device delete requested" — static string; not a moving row field. |
| `connectivity:{device_id}` | `stop_mode` | Intentional snapshot | "defer" — connectivity-loss policy; fixed at registration. |
| `health_failure:reservation:{device_id}` | `exclusion_reason` | Intentional snapshot | Captures the probe-failure description at the moment of exclusion for audit trail. |
| `health_failure:node:{device_id}` | `stop_mode` | Intentional snapshot | "graceful" stop policy fixed at health-failure detection time. |
| `maintenance:node:{device_id}` | `stop_mode` | Intentional snapshot | "graceful" stop policy; fixed at maintenance-entry time. |
| `maintenance:recovery:{device_id}` | `reason` | Intentional snapshot | Must equal `MAINTENANCE_HOLD_SUPPRESSION_REASON` exactly — `clear_maintenance_recovery_suppression` compares by value. |
| `operator:start:{device_id}` (start variant) | `desired_port` | Audit-only snapshot | Port at registration time, for the audit trail. NOT applied: the intent reconciler pins the live `node.port` (a payload port goes stale when a fallback start moves the node — the 2026-06-07 FireTV 4724↔4725 churn). |
| `operator:start:{device_id}` (restart variant) | `desired_port` | Audit-only snapshot | Same — recorded at restart time, never applied. |
| `operator:start:{device_id}` (restart variant) | `transition_token` | Intentional snapshot | Fresh UUID minted at registration for restart coordination. |
| `operator:start:{device_id}` (restart variant) | `transition_deadline` | Intentional snapshot | Window computed fresh from `appium_reconciler.restart_window_sec` at restart time. |
| `operator:stop:node:{device_id}` | `stop_mode` | Intentional snapshot | "hard" stop policy fixed at operator-stop time. |
| `operator:stop:grid:{device_id}` | _(no extra fields)_ | — | Structural only. |

**Audit note.** The `desired_port` field on `auto_recovery:node:*` was a Drop
violation (it captured `node.port`, a moving row field); it was removed in
commit `864e6feb`. The remaining `desired_port` payloads (`operator:start:*`,
`baseline:idle`) are the same moving-row-field class; rather than removing them,
the intent reconciler now ignores payload ports entirely and pins the live
`node.port` when applying a start decision — the payload copies are retained as
registration-time audit records only.

## Lifecycle Ownership

Each intent source must document every revoke path and the trigger that fires
it. Intent leaks happen when a source has multiple revoke paths and at least
one branch skips its revoke obligation. The stale-intent sweep (see
"Defense-in-depth" below) catches such leaks but the explicit paths are the
primary mechanism.

| Source | Trigger to revoke | Revoke owner(s) | Status |
|--------|-------------------|-----------------|--------|
| `active_session:{sid}` | Session ends (terminal status / finalize / Grid drop) | `update_session_status` (commit `ea9c8cbc`), `service_sync._sync_sessions`, `mark_session_finished` (commit C1) | Covered. |
| `connectivity:{device_id}` | Connectivity restored | `attempt_auto_recovery` start-node branch, `attempt_auto_recovery` early-return (commit `23561c4a`), `_crash_intents` connectivity path in `lifecycle.services.actions` | Covered. |
| `health_failure:node:{device_id}` | Recovery succeeds (node restarted) | `attempt_auto_recovery` via `revoke_intents_and_reconcile` | Covered. |
| `health_failure:recovery:{device_id}` | No longer produced | The RECOVERY-axis deny intent was removed (see `_crash_intents` comment in `lifecycle.services.actions`); recovery throttling is now governed by the backoff window plus `Device.review_required`. Retained only as a defensive revoke target in revoke `sources=[...]` lists | N/A — not registered. |
| `health_failure:reservation:{device_id}` | Device restored to run or reservation released | `restore_run_if_needed` in `lifecycle.services.actions`, `_release_device_from_run` in `service_lifecycle_release` | Covered. |
| `auto_recovery:node:{device_id}` (node_health path) | Node observed running | `node_running` precondition (`expected: False`) — reconciler sweep retires intent automatically | Covered via precondition. |
| `auto_recovery:recovery:{device_id}` (node_health path) | Node observed running | Same `node_running` precondition shared with sibling intent | Covered via precondition. |
| `auto_recovery:node:{device_id}` (lifecycle_policy path) | Node observed running | `node_running` precondition (`expected: False`) — reconciler sweep retires intent automatically | Covered via precondition (normal) / TTL `expires_at` (stale-offline branch). |
| `auto_recovery:recovery:{device_id}` (lifecycle_policy path) | Node observed running | Same `node_running` precondition shared with sibling intent | Covered via precondition (normal) / TTL `expires_at` (stale-offline branch). |
| `verification:{device_id}` | Verification probe completes (pass/fail) or TTL expiry | `_revoke_verification_node_intent` (verification execution) via `revoke_intents_and_reconcile`; `_reconcile_expired_intents` (TTL fallback) | Covered. |
| `cooldown:{axis}:{run_id}` | TTL expiry / reservation released / `restore_device_to_run` | `_reconcile_expired_intents` (TTL path), `restore_device_to_run`, `_check_expired_cooldowns` (commit `57eb770a` preserves counter), `run_active` precondition fires when run reaches terminal state | Covered. |
| `forced_release:{run_id}` | Run reaches terminal state | `run_active` precondition auto-retires intent; also cleared by `_release_device_from_run` revoke list | Covered. |
| `run:{run_id}` (grid_routing) | Reservation released | `reservation_active` precondition auto-retires intent; also cleared explicitly by `_release_device_from_run` | Covered. |
| `device_delete:{axis}:{device_id}` | Node stopped / device deleted | Device row deletion cascades `device_intents` rows via FK; also overwritten by subsequent operator start | Covered via cascade. |
| `operator:start:{device_id}` | Node observed running | `node_running` precondition (`expected: False`) — reconciler sweep retires intent automatically | Covered via precondition. |
| `operator:stop:node:{device_id}` | Operator starts the node | `_bulk_start_one` via `revoke_intents_and_reconcile(_operator_stop_sources(...))` | Covered. |
| `operator:stop:grid:{device_id}` | Operator starts the node | Same revoke call as `operator:stop:node` | Covered. |
| `maintenance:{axis}:{device_id}` | Operator exits maintenance | `maintenance_active` precondition auto-retires all three axes when the device's `maintenance_reason` is cleared (operator exits maintenance) | Covered via precondition. |

## Defense-in-depth: the Stale-Intent Sweep

Deliverable D of the state-write hardening adds `_sweep_orphaned_intents` to
the reconciler cycle (`backend/app/devices/services/intent_reconciler.py`).
The sweep revokes orphaned rows for:

- `active_session:{sid}` where the underlying `Session.ended_at IS NOT NULL`.
- `connectivity:{device_id}` where the device is not offline AND
  `device_checks_healthy IS NOT FALSE`.
- `cooldown:{axis}:{run_id}` where the underlying `DeviceReservation.released_at
  IS NOT NULL`.

The sweep exposes the Prometheus counter
`gridfleet_stale_intent_sweep_revoked_total{source="..."}`. Steady-state
expectation: the counter stays at zero. If it climbs, the explicit revoke
path for that source has a leak — find and fix the producer.
