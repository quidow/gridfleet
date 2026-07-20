# Lifecycle, Maintenance, And Recovery

This guide explains how lifecycle state differs from readiness and status, when operators should use maintenance or reconnect, and how automatic recovery behaves after failures.

## Three Different Signals

Operators see three different concepts on a device:

| Concept | Main Question |
| --- | --- |
| Readiness | Is the saved configuration verified and safe to use? |
| Operational state + Hold | Is the device available, busy, or offline, and is it blocked by a reservation or maintenance hold? |
| Lifecycle Summary | Is the manager actively recovering, deferring, suppressing, or excluding the device after device failures? |

Those signals overlap, but they are not interchangeable.

## Maintenance

Maintenance is the explicit operator-owned "do not use this right now" mode.

You can enter or exit maintenance from:

- Device Detail
- bulk device actions
- group actions

Shipped maintenance behavior:

- start and restart are blocked while the device is in maintenance
- stop remains available when a node is already running
- automatic recovery is suppressed while maintenance is active

Use maintenance when you want a clear manual hold for planned work, inspection, or hardware intervention.

## Reconnect

Reconnect is a targeted operator action for network-connected devices whose driver pack implements it (today, Android).

It is available from:

- Device Health on Device Detail
- bulk reconnect
- group reconnect

Current reconnect rules:

- the device's platform manifest must declare the `reconnect` lifecycle action
- the device must be on a network-connected lane (`connection_type=network`)
- it must have an IP address, plus an assigned host and connection target

In practice only the Android (uiautomator2) adapter implements reconnect — the Apple (xcuitest) platforms do not declare the action in their manifest — so reconnect is an Android-only operation today.

If reconnect succeeds and the device has a known node, the manager attempts a best-effort node restart afterward.

## Lifecycle Recovery States

The lifecycle summary shown on Devices and triage surfaces uses these states:

| State | Meaning |
| --- | --- |
| `Idle` | No active recovery condition is being surfaced |
| `Stopping Soon` | The manager wants to stop the device, but is waiting for the active client session to finish |
| `Waiting to Retry` | Automatic recovery previously failed and is delayed until the backoff timer expires |
| `Excluded from Run` | The device was excluded from an active run while the manager protects the run from an unhealthy member |
| `Recovery Paused` | Automatic recovery is intentionally blocked, for example by maintenance or readiness problems |
| `Start Failed` | The most recent automatic node start (Appium reconciler) failed; the device can still recover automatically when the next check succeeds |
| `Offline - Can Recover` | The device can be brought back automatically when the next checks succeed |

These are lifecycle summaries, not separate editable statuses.

## What Triggers Lifecycle Actions

The lifecycle control plane reacts to device failures such as:

- host/device connectivity loss
- repeated node-health failure
- failed session-viability probes
- CI-reported preparation failure for a reserved device

Depending on the current run/session context, the manager may:

- defer stop until the active client session ends
- stop the node immediately
- exclude the device from a reserved run
- suppress recovery because an operator state or readiness gate blocks it
- retry later with backoff
- restore the device automatically when checks succeed

## Where Operators Can Inspect Recovery State

### Devices Page

The Devices table shows a readiness/health indicator (the Health cell, which folds active lifecycle summary states — recovery paused, backoff, recoverable — into its tone and reason popover) and an Availability cell whose tooltip surfaces the maintenance reason when the device is in maintenance. There is no separate lifecycle badge column in the table; the dedicated lifecycle badge appears on the dashboard incident views.

Operators can filter the Devices table by availability status, platform, host, connection type, device type, OS version, and needs-attention.

### Device Health Panel

Device Detail shows the deeper lifecycle fields:

- recovery state
- last automatic action
- failure source
- deferred-stop status
- run exclusion state
- auto-rejoin intent
- recovery suppression reason
- next backoff time

If the exclusion came from CI preparation, the reservation and health surfaces also show the exact failure message that CI supplied for that device.

### Dashboard And Incident History

Lifecycle triage also appears in the broader fleet surfaces, especially the lifecycle-focused dashboard views and incident summaries.

## Recovery Behavior By Situation

### Active Session Is Still Running

The manager can move into `Stopping Soon` instead of killing the session immediately. The device is stopped only after the client session finishes.

### Device Belongs To An Active Run

The device can be excluded from the run so the run can continue safely while the unhealthy member is taken out of service.

This also applies when CI reports a preparation-time failure for one reserved device. In that case the manager releases the device from the run; it is placed into maintenance when `general.run_failure_escalates_to_maintenance` is enabled, otherwise left available for other runs. The exact CI-supplied reason is recorded, and healthy reserved siblings stay attached to the same run.

### Maintenance Is Active

Recovery becomes `Suppressed` because operator intent wins over automatic recovery.

### Device Is Not Ready

Recovery can also be suppressed when setup or verification gates are not satisfied. A lifecycle recovery loop cannot bypass readiness.

## Practical Operator Playbook

### Return A Device To Service

1. Resolve the hardware or transport issue.
2. Exit maintenance if maintenance was used.
3. Reconnect if the problem is a network Android transport issue.
4. Complete setup or re-verify if readiness is no longer `Verified`.
5. Start or restart the node only after readiness and maintenance blockers are clear.

### Investigate A Repeatedly Failing Device

Check, in order:

1. readiness badge and missing setup
2. Device Health checks
3. session viability result
4. lifecycle recovery state and last failure reason
5. whether the device is excluded from a run or blocked by maintenance
6. whether the reservation issue or exclusion reason shows an explicit CI preparation failure

## Troubleshooting

### Start or restart is disabled

The most common blockers are:

- the device is reserved
- the device is in maintenance
- the device is not verified

### Device keeps backing off

Automatic recovery is failing repeatedly. Use the failure reason and next backoff time in Device Health to decide whether to intervene manually.

### Device is healthy again but still unavailable

Check whether:

- it is still in maintenance
- it is still not verified
- it is excluded from an active run
- it is waiting for the backoff window to expire

### Device went into maintenance immediately after CI setup failed

That is expected when `general.run_failure_escalates_to_maintenance` is enabled (the default). When CI calls the run-scoped preparation-failure endpoint for a reserved device, the manager:

- releases the device from the run
- records the exact CI failure reason
- places it into maintenance (operational state `maintenance`) with a CI preparation-failure reason when `general.run_failure_escalates_to_maintenance` is enabled, otherwise leaves it available; healthy reserved siblings stay attached to the same run

This keeps operator-visible device state aligned with reservation truth instead of leaving the device silently reserved.
