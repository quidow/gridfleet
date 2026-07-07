# Device Intents

The control plane drives every device's desired state through the axis arbiter in
`app/devices/services/intent_evaluator.py`. For each device the reconciler
(`reconcile_device`) gathers a list of `DeviceIntent` objects — each one says
"this source wants axis X in state Y at priority P" — and the arbiter picks the
highest-priority intent per axis (`node_process`, `grid_routing`, `recovery`),
writing the derived state to the device row and its Appium node.

## Model: commands + facts

The intent list the arbiter evaluates comes from **two** places:

1. **Stored rows** in the `device_intents` table. These are the genuine
   **commands and leases** that cannot be recomputed from domain state:
   `operator:start`, `operator:stop:node`, `operator:stop:recovery`,
   `verification:{device}`, `auto_recovery:node/recovery:{device}`,
   `forced_release:{run}`, `device_delete:*`, and `health_failure:node:{device}`.

2. **Synthesized rows** built in-memory each evaluation by
   `app/devices/services/intent_synthesis.py` (`synthesize_fact_intents`) from
   domain **facts** — the reservation row, `maintenance_reason`, and
   `device_checks_healthy`. Synthesized intents are transient ORM objects; they
   are never `db.add()`-ed. `baseline:idle` (a nodeless standing start) is
   synthesized the same way, inline in `reconcile_device`.

If a desired-state effect can be recomputed from a durable fact, it is
synthesized — not stored. This keeps `device_intents` small (real commands only),
deletes the revoke-bookkeeping that stored twins required, and removes any chance
of a stored row drifting out of sync with the fact it mirrored.

## Lifecycle

A **stored** intent is deleted by exactly one of two mechanisms:

1. **Explicit revoke** via `revoke_intents_and_reconcile(...)` from the flow that
   drives the underlying state change (e.g. operator start revokes
   `operator_stop_sources`).
2. **TTL** via the `expires_at` column, swept once per reconciler tick by
   `_reconcile_expired_intents`.

There are no preconditions and no orphan sweeps. **Synthesized** intents have no
lifecycle at all — they exist only for the duration of one evaluation and
reappear (or don't) on the next tick as the underlying fact dictates.

## Synthesis table

`synthesize_fact_intents` emits one intent per satisfied fact predicate. Copy the
predicates verbatim from `intent_synthesis.py`.

| Source | Fact predicate | Axis | Payload |
|--------|----------------|------|---------|
| `run:{run_id}` | active reservation: `released_at IS NULL` and NOT indefinitely excluded (`not (excluded AND excluded_until IS NULL)`) | `grid_routing` | `{"accepting_new_sessions": true, "priority": 40}` |
| `cooldown:grid:{run_id}` | timed exclusion: `excluded AND excluded_until > now` | `grid_routing` | `{"accepting_new_sessions": false, "priority": 70}` |
| `cooldown:recovery:{run_id}` | timed exclusion (same) | `recovery` | `{"allowed": false, "priority": 70, "reason": entry.exclusion_reason}` |
| `maintenance:node:{device_id}` | `in_maintenance(device)` (`maintenance_reason` set) | `node_process` | `{"action": "stop", "priority": 80, "stop_mode": "graceful"}` |
| `maintenance:recovery:{device_id}` | `in_maintenance(device)` | `recovery` | `{"allowed": false, "priority": 80, "reason": MAINTENANCE_HOLD_SUPPRESSION_REASON}` |
| `connectivity:{device_id}` | `device_checks_healthy IS FALSE` AND no active stored `node_process` start command | `node_process` | `{"action": "stop", "priority": 50, "stop_mode": "defer"}` |

The reservation-derived intents (`run:`, `cooldown:grid`, `cooldown:recovery`)
are all recomputed from the single active `DeviceReservation` row. The exclusion
window is written **directly** on that row by the exclusion sites
(`cooldown_device`, `exclude_device_from_run`) and cleared directly by
`restore_device_to_run` / `release_device_from_run` / the
`check_expired_cooldowns` legacy sweep — the reconciler no longer echoes it.

## Semantic notes

Three behaviors changed when the precondition DSL and the stored twins were
removed:

1. **Start commands retire by TTL, not by a `node_running` precondition.** An
   `operator:start` / `auto_recovery:node` row may linger a few minutes after the
   node is running; it is a no-op while the node runs (`baseline:idle` sustains
   `running` anyway) and higher-priority stops still override it. Its TTL is
   `appium.startup_timeout_sec` + `general.session_viability_timeout_sec` + 60 s
   (auto-recovery / forced-release use `appium_reconciler.restart_window_sec`).
2. **`forced_release:{run}` retires by TTL, not a `run_active` precondition.**
   Within its short TTL a hard stop outranks an operator start (priority 95 vs 20).
3. **The synthesized `connectivity` stop is suppressed while any active stored
   `node_process` start command exists** (operator start/restart, verification
   lease, auto-recovery). This one rule replaces the old scattered "revoke
   `connectivity:*` before starting" ritual.

## Out of scope

The priority-based arbiter in `intent_evaluator.py` is unchanged — priorities
still decide which active intent wins per axis. Recovery suppression is handled
by `Device.review_required` and the lifecycle-policy backoff window, not by
intents. See [device-lifecycle.md](./device-lifecycle.md).
