# Runs And Reservations

This guide explains how reservation-backed runs behave in the product, where operators can inspect them, and when to use normal cancellation versus force release.

## What A Run Is

A run is the reservation record that locks devices for one CI or test workflow.

The manager uses it to:

- select matching devices
- mark those devices as `reserved`
- track heartbeat and TTL safety limits
- expose the reservation on Devices, Dashboard, and Runs pages
- release devices automatically when the run ends or expires

The manager does not run your test suite itself. It protects and tracks the reserved fleet slice while CI or operators do the preparation and execution work.

## Claiming Devices Inside A Run

Large CI jobs often create one run and then fan out to multiple pytest-xdist workers. Those workers can claim reserved devices one at a time instead of coordinating device assignment in client code.

Use:

```bash
curl -sf -X POST \
  "$GRIDFLEET_URL/api/runs/$RUN_ID/claim" \
  -H "Content-Type: application/json" \
  -d '{"worker_id":"gw0"}'
```

The response is one reserved device plus claim metadata:

```json
{
  "device_id": "device-uuid",
  "identity_value": "R58M...",
  "connection_target": "R58M...",
  "pack_id": "appium-uiautomator2",
  "platform_id": "android_mobile",
  "platform_label": "Android",
  "os_version": "14",
  "host_ip": "10.0.0.20",
  "claimed_by": "gw0",
  "claimed_at": "2026-04-30T12:00:00+00:00"
}
```

Claiming is atomic at the database layer. Concurrent workers either receive different devices or a `409` when no unclaimed, non-excluded reserved devices remain. A claim is a lease on an existing reservation; it does not release the device from the run.

When a worker finishes with its device but the run should remain alive, release only that claim:

```bash
curl -sf -X POST \
  "$GRIDFLEET_URL/api/runs/$RUN_ID/release" \
  -H "Content-Type: application/json" \
  -d '{"device_id":"device-uuid","worker_id":"gw0"}'
```

Release is owner-checked. The `worker_id` must match the active claim owner, which prevents one worker from accidentally releasing another worker's device. Completing, cancelling, expiring, or force-releasing the run clears all claim state while releasing the underlying reservations.

Stale claims are reclaimed lazily by the next claim request after `reservations.claim_ttl_seconds` elapses. The default is `120` seconds and can be overridden with `GRIDFLEET_RESERVATION_CLAIM_TTL_SECONDS`.

## Where Operators See Runs

`Test Runs` is the primary surface for investigating execution flows. Start there when a CI job failed or behaved unexpectedly. The `Sessions` page remains available as an advanced explorer for cross-run debugging and for sessions that predate or fall outside a run (use `Sessions → Sessions (advanced)` in the sidebar).

Use these surfaces together:

- `Runs`
  - list, filter, sort, cancel, or force release
- `Run Detail`
  - inspect timeline, TTL, heartbeat, error, reserved-device membership, and the Appium sessions that ran under this execution
- `Sessions (advanced explorer)`
  - cross-run history, ad-hoc sessions, and sessions with no associated run
- `Devices` and `Device Detail`
  - see `Reserved by <run>` ownership on affected devices
- `Dashboard`
  - monitor the `Active Runs` card and related fleet pressure

## Current Shipped Run Path

The normal reservation path today is:

1. create a run through `/api/runs`
2. matching devices become `reserved`
3. the run starts in `preparing`
4. CI prepares the reserved devices
5. if one reserved device fails preparation, CI can call `/api/runs/{id}/devices/{device_id}/preparation-failed` to exclude only that device and preserve the exact failure reason
6. CI or an operator signals `ready` after the remaining devices finish preparation
7. the run becomes `active` when either:
   - `/api/runs/{id}/active` is called explicitly, or
   - session sync observes a reserved device start work
8. the run ends as `completed`, `cancelled`, or `expired`

Run creation supports two allocation modes per requirement:

- `count`: reserve an exact number of eligible matching devices
- `allocation: "all_available"`: reserve every eligible matching device available at creation time, with optional `min_count` to fail fast when the fleet slice is too small

## Run States In Practice

| State | What It Means Today | Typical Cause |
| --- | --- | --- |
| `preparing` | devices are reserved and preparation can happen | run was just created |
| `ready` | preparation finished and the run is waiting for actual test use | CI called `ready` |
| `active` | reserved devices are in use or explicitly activated | explicit `active` or observed session use |
| `completed` | normal finish and release | CI called `complete` |
| `cancelled` | operator-initiated stop and release | `cancel` or `force-release` |
| `expired` | safety timeout released the fleet | missed heartbeat or TTL exceeded |

The UI contract also recognizes `pending`, `completing`, and `failed`, but the current run service does not normally transition runs into those states during standard operator/CI flow.

## What Reservation Does To Devices

While a run owns a device:

- the device availability becomes `reserved`
- node start, stop, and restart are blocked for direct device actions
- the Devices and Device Detail surfaces show which run owns the reservation
- lifecycle logic can exclude one device from the run if that device becomes unhealthy, while the rest of the run continues

That last point matters during triage: a run can stay alive even when one reserved member is marked excluded.

If CI explicitly reports a preparation failure for a reserved device, the manager immediately:

- excludes that device from the run
- preserves the exact CI message on the reservation as the exclusion reason
- marks the device `offline` and unhealthy
- leaves healthy reserved siblings attached to the run

## Runs Page Workflow

The `Runs` page is the fleet view for reservations.

Operators can:

- filter by run state
- filter by created date range
- sort by name, state, device count, creator, created time, or duration
- open Run Detail
- cancel active runs
- force release stuck runs

Use this page to answer:

- which runs are currently holding devices
- who created them
- how long they have been active
- whether the reservation pressure matches current fleet demand

## Run Detail Workflow

Run Detail is the incident page for one reservation.

It shows:

- TTL and heartbeat timeout
- last heartbeat and timestamps
- state timeline
- error banner when a terminal problem exists
- reserved device list, including exclusion reason when a device was removed from active participation

When a reserved device fails CI preparation, Run Detail is the primary place to confirm:

- which device was excluded
- the exact CI-supplied failure message
- whether the rest of the run can still continue with the remaining reserved devices

Use Run Detail when the Devices page tells you a device is reserved and you need to decide whether to wait, cancel, or intervene.

## Cancel vs Force Release

Use `Cancel` when:

- the run is still valid but should stop normally
- you want the reservation to end and devices to return to service

Use `Force Release` when:

- the run is stuck and the normal workflow cannot clean up
- the operator needs the devices back immediately
- you accept that the reservation is being broken administratively

Both actions release reserved devices. Force release is the stronger recovery action and leaves an admin-oriented error note on the run.

## Timeout And Safety Nets

The manager protects the fleet even if CI disappears.

Safety behaviors:

- heartbeat timeout expires a quiet run
- TTL expiry releases devices even if heartbeats continue
- startup reaper pass catches stale non-terminal runs after manager restart

Those defaults come from Settings and can be overridden per run create request.

## Practical Operator Playbook

### A run is active and everything looks normal

- leave it alone
- use Dashboard or Runs list for light monitoring

### Devices are reserved longer than expected

1. open Run Detail
2. check `Last Heartbeat`
3. compare elapsed time with TTL and heartbeat timeout
4. cancel if the workflow is still under operator control
5. force release only if the run is truly stuck

### One reserved device looks unhealthy

1. open the device
2. check whether it is excluded from the run
3. read the exclusion reason to see whether the problem came from CI preparation or backend-detected device recovery
4. use lifecycle and readiness details to understand whether the rest of the run can continue safely

## Related Guides

- [Settings And Operational Controls](settings-and-operational-controls.md)
- [Dashboard And Triage](dashboard-and-triage.md)
- [Lifecycle, Maintenance, And Recovery](lifecycle-maintenance-and-recovery.md)
- [CI Integration Guide](../ci-integration.md)
