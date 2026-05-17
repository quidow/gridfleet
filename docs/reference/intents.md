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

### `device_hold`

```json
{"kind": "device_hold", "device_id": "<uuid>", "hold": "maintenance"}
```

Satisfied while `Device.hold == hold`. Used for `maintenance:*` intents that
should clear when the operator exits maintenance.

## Adding A New Predicate

1. Add a `TypedDict` to `app/devices/services/intent_types.py` and extend the
   `Precondition` union.
2. Add a `_eval_<kind>` helper in
   `app/devices/services/intent_preconditions.py` and a branch in
   `is_satisfied`.
3. Add per-kind unit tests in `tests/test_intent_preconditions.py`.
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
