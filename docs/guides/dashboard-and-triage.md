# Dashboard And Triage

This guide explains how to read the Dashboard, which panels are best for first-pass triage, and when to drill into Devices, Runs, Hosts, or Analytics.

## What The Dashboard Is For

The Dashboard is the fastest fleet overview in the product.

Use it when you need to answer:

- is the fleet broadly healthy right now
- are runs consuming capacity
- are there devices in active lifecycle trouble
- is Grid or the database unavailable
- are hosts dropping offline

It is not the place for deep editing. It is the place to decide where to investigate next.

## Top Fleet Cards

The first row summarizes current fleet state:

- `Total Devices`
- `Available`
- `Busy`
- `Offline`
- `Active Sessions`
- `Queued`

Read these cards together, not in isolation.

Examples:

- high `Queued` with normal `Available` often means Grid/node startup lag
- high `Offline` points more toward host or transport trouble
- high `Busy` with low queue is usually healthy expected load

## Seven-Day Summary Cards

The second row is trend-oriented rather than moment-oriented.

It includes:

- `Pass Rate (7d)`
- `Fleet Utilization (7d)`
- `Devices Needing Attention`

`Devices Needing Attention` links to Analytics reliability views. Use that when the Dashboard tells you there is chronic pain, not just a live incident.

## Lifecycle Triage

The `Lifecycle Triage` panel is the most important operator panel for current recovery work.

It has two parts:

- state chips with counts
- recent lifecycle incidents

The chips link straight to Devices filtered by lifecycle summary state such as:

- deferred stop
- backoff
- excluded
- suppressed
- recoverable
- manual

Use this workflow:

1. check which lifecycle state is growing
2. open the matching Devices filter
3. inspect one affected device in Device Detail
4. clear the blocker: maintenance, readiness, reconnect need, host issue, or repeated recovery failure

## Active Runs

The `Active Runs` card answers whether reservation pressure is expected.

Use it when:

- Devices look reserved and you need quick attribution
- queue pressure may be caused by current CI activity
- you need to jump from fleet view to one specific run

This card is a summary only. Switch to `Runs` for real run management.

## System Health Row

The health row separates system-layer issues from device-layer issues.

### Database

`System Health` shows whether the backend can reach the database.

If this is disconnected, stop treating device symptoms as isolated fleet issues until the backend is healthy again.

### Grid Health

The Dashboard translates raw Grid status into operator language:

- `Ready`
  - Grid is accepting traffic
- `Starting`
  - Grid is reachable and nodes are still registering
- `Waiting for Nodes`
  - Grid is reachable but no nodes are registered yet
- `Idle`
  - Grid is reachable and simply waiting for work
- `Unavailable`
  - Grid could not be reached or returned an error

Use this panel before blaming a single device for broad Appium-session failures.

### Hosts

The `Hosts` card summarizes total, online, and offline host count.

If several devices appear offline at once, compare that symptom to host offline count before digging into each device individually.

## Active Devices Table

The final table lists currently `busy` devices.

Use it to:

- confirm which devices are in live use
- jump to a busy device quickly
- separate active test traffic from unhealthy idle inventory

## Practical Triage Playbook

### Queue is rising

1. check `Active Runs`
2. check `Grid Health`
3. check whether `Available` is low or whether nodes are just missing

### Devices are failing one by one

1. check `Lifecycle Triage`
2. open recent incidents
3. drill into one representative Device Detail

### Many devices go offline together

1. check `Hosts`
2. confirm whether host offline count increased
3. move into Hosts and Host Detail before touching individual devices

## Related Guides

- [Runs And Reservations](runs-and-reservations.md)
- [Hosts And Host Detail Operations](hosts-and-host-detail-operations.md)
- [Lifecycle, Maintenance, And Recovery](lifecycle-maintenance-and-recovery.md)
