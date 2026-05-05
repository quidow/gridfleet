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

Reconnect is a targeted operator action for network-connected Android devices.

It is available from:

- Device Health on Device Detail
- bulk reconnect
- group reconnect

Current reconnect rules:

- Android platforms only
- network-connected lanes only
- device must have an IP address

If reconnect succeeds and the device is auto-managed with a known node, the manager attempts a best-effort node restart afterward.

## Lifecycle Recovery States

The lifecycle summary shown on Devices and triage surfaces uses these states:

| State | Meaning |
| --- | --- |
| `Idle` | No active recovery condition is being surfaced |
| `Deferred Stop` | The manager wants to stop the device, but is waiting for the active client session to finish |
| `Backing Off` | Automatic recovery previously failed and is delayed until the backoff timer expires |
| `Excluded` | The device was excluded from an active run while the manager protects the run from an unhealthy member |
| `Suppressed` | Automatic recovery is intentionally blocked, for example by maintenance or readiness problems |
| `Recovery Eligible` | The device can be brought back automatically when the next checks succeed |
| `Manual Recovery` | The device has recovery work remaining, but auto-manage is not allowed to bring it back automatically |

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

The Devices table shows:

- readiness badge
- lifecycle badge when the summary is active
- lifecycle detail text for the current summary

The lifecycle chips also let operators filter directly by lifecycle state.

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

The manager can move into `Deferred Stop` instead of killing the session immediately. The device is stopped only after the client session finishes.

### Device Belongs To An Active Run

The device can be excluded from the run so the run can continue safely while the unhealthy member is taken out of service.

This also applies when CI reports a preparation-time failure for one reserved device. In that case the manager excludes the device from the run, marks it `offline`, records the exact CI-supplied reason, and keeps healthy reserved siblings attached to the same run.

### Auto-Manage Is Disabled

The device can land in `Manual Recovery`. Operators must bring it back intentionally instead of waiting for automatic recovery.

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

### Device shows manual recovery instead of recovering automatically

Auto-manage is off, or another suppression rule is preventing automatic recovery. Operators need to return the device to service intentionally.

### Device is healthy again but still unavailable

Check whether:

- it is still in maintenance
- it is still not verified
- it is excluded from an active run
- it is waiting for the backoff window to expire

### Device went offline immediately after CI setup failed

That is expected for the current shipped contract. When CI calls the run-scoped preparation-failure endpoint for a reserved device, the manager:

- excludes the device from the run
- records the exact CI failure reason
- marks the device unhealthy and sets operational state to `offline`

This keeps operator-visible device state aligned with reservation truth instead of leaving the device silently reserved.
