# Doc 5 — Allocations, Ports, and Sessions

> Cross-cutting reference for the resources a node grabs at start and gives back at stop: owner allocations, Appium ports, Grid sessions, and run/reservation integration.

These resources are easy to leak. Most of the bugs that look like "node won't restart" or "Grid still routes to dead device" are actually leaks here — a port that nobody released, a Grid session that survived its run, or an owner bundle still claiming capabilities for an orphan process. This doc captures the lifecycle for each so we know who frees what, when.

## Three things called "session"

Before anything else, disambiguate:

| Name | What it is | Lives in | Lifetime |
| --- | --- | --- | --- |
| **WebDriver session** | The W3C session opened by a client against the Grid hub | Selenium Grid + downstream Appium | from `POST /session` to `DELETE /session/{id}` |
| **`Session` row** | DB row created by `session_sync_loop` from Grid's `/status` | `sessions` table | recorded for the run's life + retention |
| **Run reservation** | Operator/CI hold on one or more devices for a test run | `device_reservations` table | from reserve to run completion/cancel |

A WebDriver session is what consumes a node. A `Session` row is the manager's record that one is in flight. A reservation is independent of any session and may exist before any client connects.

The split matters because the **reapers are different**:

- `session_sync_loop` reaps `Session` rows whose Grid session no longer exists.
- `run_reaper_loop` reaps abandoned runs and explicitly calls `grid_service.terminate_grid_session(...)` on each device's Grid session before clearing the reservation.
- Operator stop/restart of a node never touches Grid sessions directly — Appium's own teardown is what cancels them.

This doc focuses on resource ownership across those three.

## The owner-allocation model

Before a node starts, the manager allocates a **bundle** of resources for the device, keyed by an `owner_key`. The bundle is stored in the control-plane KV namespace `appium.parallel.owner` (`backend/app/services/appium_resource_allocator.py:26`).

```mermaid
classDiagram
    class OwnerBundle {
      string owner_key
      uuid host_id
      string allocation_key
      dict capabilities
      list[Claim] claims
      datetime claimed_at
    }
    class Claim {
      string namespace
      string key  // the claimed numeric port as string
    }
    OwnerBundle --> "many" Claim : holds
```

Two flavours of `owner_key`:

| Key shape | When | File |
| --- | --- | --- |
| `device:<uuid>` (`managed_owner_key`) | Stable allocation for a managed device — used by `start_node`, `restart_node` | `appium_resource_allocator.py:36-37` |
| `temp:<host>:<identity>` (`temporary_owner_key`) | Verification probe; transient | `appium_resource_allocator.py:40-45` |

A bundle holds:

- **`capabilities`** — the per-host parallel resource ports the pack manifest declares. Each device that the pack supports gets its own port (e.g. `mjpegServerPort`, `chromedriverPort`). Values come from `resolve_pack_platform(...).parallel_resources.ports`. The capabilities are merged into the agent start payload and shipped down with the Appium spawn.
- **`claims`** — KV records that prove this owner currently holds those ports. Each claim is an entry in `appium.parallel.claim.<host>.<capability>` with `{owner_key, claimed_at}`. Reading the namespace tells the allocator which numeric values are taken.
- **`allocation_key`** — random hex used for `appium:derivedDataPath` (XCUITest) so two iOS devices don't share a derived-data dir.

### Allocate

```mermaid
sequenceDiagram
    autonumber
    participant Caller as start_node / verification
    participant Alloc as appium_resource_allocator
    participant Kv as control_plane_state_store

    Caller->>Alloc: get_or_create_owner_capabilities(owner_key, host_id, resource_ports)
    Alloc->>Kv: get_value(OWNER_NAMESPACE, owner_key)
    alt Existing bundle on same host
        Alloc-->>Caller: existing capabilities
    else None or different host
        Alloc->>Kv: release_owner (if existing)
        loop for each resource port
            loop offset 0..POOL_SIZE
                Alloc->>Kv: try_claim_value(claim_ns, candidate)
                alt claimed
                    Note over Alloc: record claim, capability=candidate
                end
            end
        end
        Alloc->>Kv: set_value(OWNER_NAMESPACE, owner_key, bundle)
        Alloc-->>Caller: capabilities
    end
```

`POOL_SIZE = 1000` per port (start at the manifest-declared base, walk up to `+999`). On any failure mid-allocation, all already-acquired claims are rolled back (`appium_resource_allocator.py:186-189`). The bundle never lands in the OWNER namespace until every required port is claimed.

### Reuse

If a caller asks for `get_or_create_owner_capabilities` with the same `owner_key` on the same host, the existing bundle is returned untouched. This is what makes `restart_node` cheap: the same owner key persists across the stop→start, and the second call is a no-op rather than a re-allocation.

### Release

`release_owner(owner_key)`:

1. Read the bundle.
2. Delete every claim record (`claim_namespace.<host>.<capability>:<value>`).
3. Delete the bundle itself.

The order is intentional: a partially-released bundle would still be visible while its claims are gone, opening a window where a fresh allocator could reuse the values. Doing it claims-first keeps the invariant "if a bundle exists, its ports are claimed".

### Critical rule — release on confirmed stop only

`stop_temporary_node` (`backend/app/services/node_manager.py:241-271`):

```text
release_owner(...) is awaited ONLY when the agent acknowledged the stop.
```

The reason — restating Doc 2's split-brain rule from the allocation angle — is that an unacknowledged stop may leave a real Appium process holding the port at the OS level. If the bundle were released anyway, the allocator would hand the same port to a new owner. The next `start_node` then issues `/agent/appium/start` with that port, the agent sees the orphan listening, and the start fails with "already in use" — but on a port we just told the bundle was free. Commit `bdfae85` enforces this rule in code; `restart_node_via_agent` (`node_manager_remote.py:289-313`) follows the same pattern.

### Transfer

`transfer_owner(source, target)` — re-keys an existing bundle to a new owner without releasing claims. Used when a verification probe (temporary owner) is promoted to a managed node (managed owner) without an intermediate teardown.

## Port allocation

Two ranges, two owners:

| Range | Owner | Purpose | Default |
| --- | --- | --- | --- |
| `appium.port_range_start..appium.port_range_end` | manager (DB-tracked via `AppiumNode.port`) | one Appium server per managed node | `4723..4823` |
| `AGENT_GRID_NODE_PORT_START` upward | agent (host-local) | one Selenium Grid relay per Appium node | per-host setting |
| Per-device parallel resources (e.g. `mjpegServerPort`, `chromedriverPort`) | owner allocator (KV-tracked) | extra Appium-side ports the pack manifest declares | depends on manifest |

Only the first range is the "main" Appium port. The other two come into play after `/agent/appium/start` succeeds and Appium spawns its own helpers.

### `candidate_ports`

`backend/app/services/node_manager_state.py:41-72`:

```text
1. used = ports of AppiumNode rows where state = running
2. excluded = caller-provided exclude set (e.g. ports we already tried this attempt)
3. for port in [start..end]:
     if port in [used ∪ excluded]: skip
     else: candidate
4. preferred port (if free) goes first; the rest follow in numeric order
```

The DB row, not the agent, is the authority for "is this port in use by us". External listeners on a port in the managed range are detected only at start time, when the agent rejects with "already in use".

### Port conflict recovery

```mermaid
flowchart LR
    A[start request] --> B{candidate_ports}
    B --> C{try start on port}
    C -->|2xx| D[mark_node_started]
    C -->|already in use| E[map to NodePortConflictError]
    E --> F{more candidates?}
    F -->|yes| C
    F -->|no| G[raise NodePortConflictError]
```

`_start_with_owner` (`node_manager.py:107-169`) iterates candidates until one succeeds or the pool is exhausted. The rule from commit `54707d1` — agent drops stale node state on a managed-port conflict — is what makes this loop converge: an agent that was bouncing requests on the same port should accept the next attempt instead of permanently rejecting.

The `restart_node_via_agent` path uses the same loop but starts from the **previous port** as the preferred candidate (`node_manager_remote.py:384`). This minimises Grid registration churn: usually we restart on the same port and Grid does not need to re-discover the relay.

## Grid sessions and Selenium Grid registration

Each Appium node is paired on the agent host with a Selenium Grid relay process — a Java sidecar that registers the Appium server with the central hub. The backend sees this only indirectly via `grid_service.get_grid_status()` (`backend/app/services/grid_service.py:11`), which fetches `/status` from the hub.

Two consequences for the lifecycle:

1. **Started Appium ≠ usable node.** A successful `/agent/appium/start` returns 2xx as soon as Appium is alive, but the Grid relay registration is asynchronous on the agent side. `node_health_loop` gives a "registration grace window" equal to `appium.startup_timeout_sec` before treating "not in Grid status" as a failure (`node_health.py:153-160, 232-247`). Inside that window the snapshot stays at `running=True`.

2. **Stopped Appium ≠ no Grid registration.** Killing the Appium process should also tear down the Grid relay, but only if the agent acknowledged the stop. An orphan Appium plus its still-registered relay means Grid will keep routing sessions to it. This is the operational reality behind the commit `4171847` rule: do not flip the DB to `stopped` without ack, because Grid is still using the slot.

`available_node_device_ids` (`grid_service.py:44-73`) extracts the set of GridFleet-tagged device IDs from `/status` so loops can see "what does Grid think is available right now" without scraping HTML.

### Reaping a Grid session

`grid_service.terminate_grid_session(session_id)` issues `DELETE /session/{id}` to the hub. A 404 is treated as success (the session was already gone). Used by:

- `run_reaper_loop` — when an abandoned run is being closed out, every active Grid session for the run's devices is terminated explicitly. This is the change from commit `54707d1` that stopped sessions surviving their owning run.
- `session_sync_loop` — when reconciling DB session state against Grid `/status`, sessions that the DB has marked terminal but Grid still shows are removed.

## Reservations and run integration

```mermaid
sequenceDiagram
    autonumber
    participant Client as CI / operator
    participant Run as run_service
    participant Pg as Postgres
    participant Sync as session_sync_loop
    participant Grid as Selenium Grid
    participant Reaper as run_reaper_loop

    Client->>Run: POST /api/runs (capabilities, count)
    Run->>Pg: insert TestRun + DeviceReservation rows
    Run->>Pg: lock_devices + set_device_availability_status(reserved)
    Note over Pg: Devices flip available → reserved
    Client->>Grid: WebDriver POST /session against reserved device
    Grid-->>Client: session id
    Sync->>Grid: GET /status
    Grid-->>Sync: list of active sessions
    Sync->>Pg: insert Session row, link to run, flip reserved → busy
    Client->>Grid: DELETE /session/{id}
    Sync->>Grid: GET /status
    Grid-->>Sync: empty
    Sync->>Pg: mark Session ended, restore_post_busy_availability_status
    Note over Pg: Devices flip busy → reserved (run still active) or available

    alt Run completes normally
      Run->>Pg: TestRun.state=completed
      Run->>Pg: clear DeviceReservation rows + flip reserved → available
    else Run abandoned (no signal in time)
      Reaper->>Grid: terminate_grid_session for each device's open session
      Reaper->>Pg: TestRun.state=expired/failed, clear reservations, flip reserved → available
    end
```

Key facts:

- `availability_status = reserved` is the **run's** hold on a device, separate from any active session. Stays `reserved` between sessions while the run is alive.
- `available → reserved` flips happen when the run is created. `reserved → available` flips happen when the run completes/cancels OR when the device is excluded from the run for health reasons (lifecycle policy).
- `reserved → busy` is the per-session flip done by `session_sync_loop`. The reverse is the `restore_post_busy_availability_status` helper (`device_availability.py:66-75`) which respects an active reservation.
- `node_health_loop` skips `reserved` and `busy` devices (`_should_probe_node_health` only allows `available`, `node_health.py:83`). So a device under a run is invisible to auto-restart while it is being driven.

## Failure-mode glossary (resource leaks)

| Symptom | Likely leak | Fix surface |
| --- | --- | --- |
| `start_node` keeps failing with "already in use" but the DB row says `stopped` | Released owner allocation while orphan still running; allocator handed the port back | Gate `release_owner` on confirmed stop (commit `bdfae85`) |
| Two relays registered for the same device on Grid | Restart issued before stop ack; orphan + new node both alive | Refuse to start during restart unless stop is acknowledged (commit `4171847`) |
| Device shows `reserved` forever after run abandoned | `run_reaper_loop` did not run (leader down? frozen?) or grid session terminate failed | Inspect leader state; manually `DELETE /session/{id}` via Grid hub or use lifecycle exclusion |
| `Session` row stays `running` after Grid session ended | `session_sync_loop` skipped a tick | Reaper retries on next cycle; only escalate if persistent |
| Owner bundle exists but no `AppiumNode` row | `mark_node_started` never ran (start failed after agent OK but before commit) | `release_owner` on next operator action; consider auto-cleaning bundles whose host has no matching node row in `data_cleanup_loop` |
| Port range exhausted | `release_owner` skipped on stop, slow leak | `candidate_ports` raises `NodeManagerError("No free ports available...")`. Audit the allocator namespace |

The recurring pattern: the device row, the `AppiumNode` row, the owner bundle, and the agent process must all agree on "is this device served right now". When they disagree, you have a leak. The split-brain rules in Doc 2 keep them aligned at write time; the reapers in Doc 3 catch what slips through.

## Sequencing rules summary

For new code that touches these resources, follow this order:

1. **Acquire.** Allocate owner bundle BEFORE asking the agent to start. Released only on confirmed stop.
2. **Verify.** After agent says OK, poll `/agent/appium/{port}/status` until ready. Only then write DB state.
3. **Persist.** `mark_node_started` writes `AppiumNode`, `Device.availability_status`, and the health snapshot in one transaction.
4. **Release on stop.** Agent ack required for `release_owner`, `mark_node_stopped`, and the snapshot flip.
5. **Reap on abandonment.** Loop-driven cleanup uses `terminate_grid_session` + state restore, not direct DB writes that bypass the helpers.

If a code path skips one of these steps it will eventually leak, and the symptoms will look exactly like the failure-mode rows above.

## What this doc does NOT cover

- Multi-axis device state — see Doc 1.
- The DB↔agent ack contract for node lifecycle — see Doc 2.
- Loop cadence and tri-state probe — see Doc 3.
- HTTP shapes and circuit breaker — see Doc 4.
