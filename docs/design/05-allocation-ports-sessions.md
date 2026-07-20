# Doc 5: Allocations, Ports, Sessions

> Cross-cutting reference for the resources a node consumes: typed resource claims, Appium ports, WebDriver sessions, and run/reservation integration. Most bugs that look like "node won't restart" or "router still allocates a dead device" are leaks here.

## Three things called "session"

| Name | What it is | Lives in | Lifetime |
| --- | --- | --- | --- |
| **WebDriver session** | The W3C session a client opens against the router, proxied to the allocated device's Appium | router + downstream Appium | from `POST /session` to `DELETE /session/{id}` |
| **`Session` row** | The manager's record that an Appium session is in flight. Created by the backend at claim time — for client sessions and probes alike: *every Appium session the control plane commissions has a `Session` row from birth* (WS-16.1) | `sessions` table | claim → terminal + retention |
| **Run reservation** | Operator/CI hold on one or more devices for a test run | `device_reservations` table | reserve → run completion/cancel |

The reapers are different, which is why the split matters:

- The `appium_sweep` sync pass closes `Session` rows whose Appium session no longer exists, and kills orphan Appium sessions that have no row.
- Abnormal run release (`RunLifecycleService.cancel_run`, `.force_release`, `.expire_run` — the last driven by the janitor's `run_reaper` stage via `reap_stale_runs`) terminates each running session directly against the device's Appium (`appium_direct.terminate_session`) before clearing the reservation. Normal `complete_run` does not terminate sessions; the client owns normal WebDriver teardown.
- Operator stop/restart of a node never touches sessions; Appium's own teardown cancels them.

## The typed allocation model

Appium parallel resources live in `appium_node_resource_claims` (model in `app/appium_nodes/`): one row per claim, keyed by `(host_id, capability_key, port)` with a partial unique index enforcing one managed row per `(node_id, capability_key)`. Every claim is inserted already bound to a `node_id` — there is no temporary/promotion step — and its lifetime is the node row's: `ON DELETE CASCADE` frees claims only when the node (or its device) is deleted. `release_managed(node_id)` / `release_capability(node_id, capability_key)` exist for explicit early release. Non-port managed capabilities (e.g. XCUITest `appium:derivedDataPath`) live in `appium_nodes.live_capabilities` and are merged with port claims by `get_capabilities(node_id)`; `set_node_extra_capability` writes them.

**Why the FK matters.** The predecessor KV bundle had two correctness rules to remember ("release claims before the bundle", "only release on confirmed stop"). The first is now structural — the cascade is atomic — and the second reduces to a single DELETE.

## Port allocation

| Range | Owner | Purpose |
| --- | --- | --- |
| `appium.port_range_start..appium.port_range_end` | manager (DB-tracked via `AppiumNode.port`) | one Appium server per managed node |
| Per-device parallel resources (e.g. `mjpegServerPort`, `chromedriverPort`) | typed claim table | extra ports the pack manifest declares |

The DB row, not the agent, is the authority for "is this port in use by us": `candidate_ports` (`reconciler_allocation.py`) excludes ports of nodes that are observed-running or desired-running on the target host, preferred port first. The `used` set is host-scoped — two hosts can both run Appium on the range start without colliding. External listeners are discovered only agent-side at spawn time and ride back as `start_failures` (`port_conflict`), which re-pins `desired_port` — Doc 2 owns that convergence.

## WebDriver sessions and routing

There is no Grid hub and no per-node relay. The router listens on `:4444`; for each new `POST /session` it calls the backend's internal grid API (`/internal/grid/create-session`, `app/grid/router_internal.py`). The backend claims a device — candidates are projection-`available` devices whose node is viable (`node_viable_predicate`) and accepting new sessions (`node_accepting_new_sessions_predicate`) in `_eligible_devices_with_facts` (`app/grid/allocation.py`) — **creates the Appium session itself** (`app/grid/session_create.py`; session creation is backend-owned, there is no router confirm step), records the `Session` row, and returns the target. The router then proxies that session's W3C commands directly to the device's Appium.

Rows end primarily via the router's `/internal/grid/sessions/ended` notification (`AllocationService.mark_ended`); the `appium_sweep` sync pass is the liveness backstop, closing rows through `close_running_session` when a session disappeared without a notification, and killing Appium sessions that have no row. Probe sessions participate in all of this like any other row — a probe orphaned by scheduler death is an ordinary crash-orphaned running row — with one deliberate projection rule: probe rows claim the device but are excluded from the `busy` masking input, and they emit no `session.*` events.

Crash-orphaned *pending* rows and stale queue tickets are failed/expired by `grid_allocation_reaper_loop` (`app/grid/allocation_reaper.py`).

Two lifecycle consequences:

1. **Started Appium ≠ usable node.** The agent starts Appium as soon as it pulls a desired node; the pushed node-health section is the authoritative liveness signal (Doc 3). There is no registration step and no grace window.
2. **Stopped Appium must be confirmed.** An orphan Appium still listening on its managed port stays reachable by the router. The DB never flips to stopped without the agent's report proving the process gone (Doc 2).

## Reservations and run integration

- A **reservation** is the run's hold on a device: a `DeviceReservation` row inserted at run creation (computed `is_reserved` derives from it), independent of any session, cleared on run completion/cancel or health-driven exclusion. The run allocator locks candidates with `SELECT ... FOR UPDATE SKIP LOCKED` (`_find_matching_devices`).
- Nobody writes `busy` anywhere: a live `Session` row (pending counts) is a durable fact the read-time projection folds into `busy`; when the row terminates, reads derive `available`/`offline` again, reservation untouched.
- **Run-routed sessions.** Run membership is enforced at allocation: sessions bound to a run arrive through the router's run-scoped endpoint (`/run/{run_id}`), and `_ticket_passes_reservation` (`app/grid/allocation.py`) admits a candidate only when the ticket's run id matches the device's active reservation — strictly symmetric (run-bound sessions only to that run's devices; reserved devices only to that run's sessions). The testkit composes the run-scoped URL from `GRIDFLEET_RUN_ID`; sessions without it land on unreserved devices only. The legacy `gridfleet:run_id` capability is rejected with an explicit error.
- Deploy order: upgrade the backend before (or together with) the router — an old backend silently ignores the run id on the internal allocate call.

## Failure-mode glossary (resource leaks)

| Symptom | Likely cause | Fix surface |
| --- | --- | --- |
| Repeated `port_conflict` start failures on the same port | External listener in the managed range, or an unconfirmed orphan | Re-pin converges automatically; audit the host if it recurs (Doc 2) |
| Two Appium processes alive for one device | A code path bypassed the watermark/pull rules | Doc 2's checklist; the agent's own orphan reap converges it |
| Device shows reserved forever after a run was abandoned | Janitor `run_reaper` stage not running (scheduler down?) or terminate failed | Check scheduler health; `DELETE /session/{id}` against the device's Appium; lifecycle exclusion |
| `Session` row stays `running` after the Appium session ended | Ended-notification lost and the sync pass hasn't caught up | The backstop closes it on the next cycle; escalate only if persistent |
| Port range exhausted | Stale `appium_node_resource_claims` rows for undeleted nodes | `candidate_ports` raises `NodeManagerError`; audit the claim table |

The recurring pattern: the device row, the node row, the typed claims, and the agent process must all agree on "is this device served right now". Doc 2's rules keep them aligned at write time; the reapers here catch what slips through.

## Sequencing rules summary

1. **Acquire claims bound to the `node_id`** (via `appium_node_resource_service`) before the desired-state write that will make the agent spawn against them.
2. **Write desired state under the locks** (Doc 2's order) and poke; never call the agent to start/stop.
3. **Never write observed state speculatively** — the pushed report is the only confirmation.
4. **Claims live with the row.** Release only on node/device deletion (or an explicit confirmed-stop `release_managed`).
5. **Reap through facts:** loop cleanup uses `terminate_session` + durable session facts, never direct projection writes.

## What this doc does NOT cover

- Multi-axis device state: see Doc 1.
- The DB↔agent contract for node lifecycle: see Doc 2.
- Loop cadence and the tri-state probe: see Doc 3.
- HTTP shapes and the circuit breaker: see Doc 4.
