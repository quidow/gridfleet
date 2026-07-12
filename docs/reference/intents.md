# Device Intents

The control plane drives every device's desired state through the explicit
decision ladders in `app/devices/services/decision.py`. For each device the
reconciler (`reconcile_device`) parses the stored `DeviceIntent` rows into typed
**commands**, gathers domain **facts** once (`gather_decision_facts`), and calls
three pure deciders — node process, grid routing, and recovery — that write the derived state to the device row and its Appium
node.

## Model: commands + facts

`desired = f(stored_commands, facts)`. There are two inputs, kept strictly apart:

1. **Stored rows** in the `device_intents` table are the genuine **commands and
   leases** that cannot be recomputed from domain state. A row is `(source, kind,
   payload, run_id, expires_at)`: `source` is the per-device deduplication and
   revocation key, while `kind` is the `CommandKind` value that selects the
   command. `parse_command` reads `kind`; unknown kinds are logged and ignored.

2. **Facts** are read directly from domain rows by `gather_decision_facts` — never
   re-encoded as intent rows. They are: `in_maintenance` (maintenance_reason set),
   `device_checks_unhealthy` (`device_checks_healthy IS FALSE`), `in_service`
   (baseline eligibility, F-G1), the single active `DeviceReservation` row
   (which yields `reservation_run_id`, `cooldown_active`, `cooldown_reason`), and
   the remediation-log **node-process directive** (`NodeDirective`, derived by
   `derive_ladder` from `auto_stop_commissioned` / `restart_commissioned` /
   `recovery_started` rows — newest-in-episode wins).

Precedence is the **ordered code** in the deciders, not a numeric priority in the
payload. Payloads carry only what a decider reads.

## Precedence (node_process ladder)

`decide_node_process` returns at the first rung that matches, mirroring the
retired numeric ladder exactly:

1. `operator:stop:node` command → hard stop.
2. `forced_release` command → hard stop.
3. `in_maintenance` **fact** → graceful stop (`maintenance hold`).
4. remediation **stop directive** (fact) → graceful stop, structurally suppressed by any active start (stored or derived).
5. `device_checks_unhealthy` **fact**, and no active start → `running_blocked` (connectivity park), same suppression.
6. any start — stored `operator:start` / `verification` commands plus the derived remediation **start directive** (`in_service`-gated) → running. A restart-bearing start (one carrying `restart_requested_at`) beats a plain standing order; among restarts the newest watermark wins (a later request supersedes an earlier one) and the derived directive's watermark folds into the same newest-wins fold; ties break lexicographically by source.
7. `in_service` **fact** (no commands) → running (`baseline:idle` standing start).
8. otherwise → stopped.

Grid routing is pure fact: no reservation → accept; active reservation →
route the run; cooldown → keep the run bound but block new sessions.

`decide_recovery`: `operator:stop:recovery` command denies; else `in_maintenance`
denies (with `MAINTENANCE_HOLD_SUPPRESSION_REASON`); else `cooldown_active`
denies (with the exclusion reason); else default-allow.

The recovery decision is consumed **read-side**, not cached by the reconciler: the
reconciler derives node-process and grid-routing state only. `decide_recovery` is
folded into `app.devices.services.recovery_projection.recovery_availability` (the
read-time "can recovery act now" projection consulted by the write path and the
lifecycle-policy badge) and into the presenter's `derived.recovery` debug view.
There is no `Device.recovery_allowed` / `recovery_blocked_reason` column — the
decision is recomputed at read time (see `docs/reference/device-lifecycle.md`).

## Per-source payload table

Stored command payloads carry only the fields a decider reads. `action` (and
`allowed` on recovery commands) are kept so rows stay self-describing in the
debug view at zero decision cost. Stop mode is implied by the command kind;
`priority` and `desired_port` are not stored.

| Source | `CommandKind` / stored `kind` | Payload |
|--------|-------------------------------|---------|
| `operator:start:{device_id}` | `operator:start` | `{"action": "start"}` (restart variant adds `restart_requested_at`) |
| `operator:stop:node:{device_id}` | `operator:stop:node` | `{"action": "stop"}` |
| `operator:stop:recovery:{device_id}` | `operator:stop:recovery` | `{"allowed": false, "reason": "Operator stopped the node"}` |
| `forced_release:{run_id}` | `forced_release` | `{"action": "stop"}` |
| `verification:{device_id}` | `verification` | `{"action": "start"}` |

The three retired system pseudo-commands (`health_failure:node`,
`auto_recovery:node`, `auto_recovery:recovery`) are no longer stored rows — their
decisions derive from the remediation-log node-process directive (see the facts
list above and [device-lifecycle.md](./device-lifecycle.md)). The table now lists
**external will only**.

## Lifecycle

A stored command is deleted by exactly one of two mechanisms:

1. **Explicit revoke** via `revoke_intents_and_reconcile(...)` from the flow that
   drives the underlying state change (e.g. operator start revokes
   `operator_stop_sources`; the derived failure-stop directive is superseded by an
   appended `reset` row, not a revoke).
2. **TTL** via the `expires_at` column, swept by `_gc_expired_intents` at the
   start of every reconciler tick (a bulk delete; the every-tick full scan
   re-derives the affected devices).

There are no preconditions and no orphan sweeps. Facts have no lifecycle at all —
they are read fresh each tick, so a decision reappears (or doesn't) as the
underlying fact dictates.

## Semantic notes

1. **`operator:start` retires by TTL, not by a `node_running` precondition.** The
   row may linger a few minutes after the node is running; it is a no-op while the
   node runs (`baseline:idle` sustains `running`) and stops higher on the ladder
   still override it. Its TTL is `appium.startup_timeout_sec` +
   `general.session_viability_timeout_sec` + 60 s (forced-release uses
   `appium_reconciler.restart_window_sec`).
2. **`forced_release:{run}` retires by TTL, not a `run_active` precondition.**
   Within its short TTL a hard stop outranks an operator start on the ladder.
3. **The connectivity park and the derived failure-stop are both suppressed while
   any active start exists** — a stored command (operator start/restart,
   verification lease) or the derived remediation start directive. This structural
   rule (rungs 4–5 check `not starts and not remediation_start`) replaces the old
   scattered "revoke `connectivity:*` / `failure_stop_sources` before starting"
   rituals.

4. **2026-07-12 (WS-15.2): the system pseudo-commands were demoted to log-derived
   rungs**, completing the 2026-07-09 demote-facts-from-intents direction. The
   TTL-GC sweep now serves only the external-will survivors (verification lease,
   `forced_release`, `operator:start`).

## Out of scope

Recovery suppression is also governed by `Device.review_required` and the
lifecycle-policy backoff window, not by intents. See
[device-lifecycle.md](./device-lifecycle.md).
