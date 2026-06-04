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

The first row is three clickable stat cards that summarize current fleet state:

- `Hosts` — links to `/hosts`; hint shows `online/total online`
- `Devices` — links to `/devices`; hint shows `N available · M offline`
- `Sessions` — links to `/sessions`; hint shows `N running · M queued`

Availability, offline, and queue figures live in the card hints rather than as separate cards. Busy is not a top-row figure; busy devices appear in the Operations section instead.

Read these cards together, not in isolation.

Examples:

- a high queued count with normal availability often means Grid/node startup lag
- a high offline count points more toward host or transport trouble
- many devices busy with a low queue is usually healthy expected load

## Seven-Day Summary Tiles

These are trend-oriented rather than moment-oriented. They are not a separate row: they are the three `Last 7 days` `MetricTile`s in the first column of the Operations section (the last block on the dashboard).

The tiles are:

- `Pass rate` — links to `/analytics`
- `Fleet utilization` — links to `/analytics`
- `Reliability watchlist` — its value is the devices-needing-attention count; links to `/analytics?tab=reliability`

Use `Reliability watchlist` when the Dashboard tells you there is chronic pain, not just a live incident.

## Device Recovery

The `Device recovery` card is the most important operator panel for current recovery work.

It has two parts:

- an affected-device count plus a list of affected devices, each carrying a lifecycle badge (linking to the device detail page, with a `+N more affected` link to `/devices?needs_attention=true`)
- a `Recent incidents` list of recent lifecycle events

The lifecycle summary states surfaced as badges are `idle`, `deferred_stop`, `backoff`, `excluded`, `suppressed`, and `recoverable`. These are badges, not filter chips — the Devices page has no lifecycle-summary-state filter. The card's links go to device detail (`/devices/{id}`), `/devices?needs_attention=true`, and `/analytics?tab=reliability`.

Use this workflow:

1. scan the affected-device list and recent incidents for what is growing
2. open `/devices?needs_attention=true` or the device detail page from the card
3. inspect one affected device in Device Detail
4. clear the blocker: maintenance, readiness, reconnect need, host issue, or repeated recovery failure

## Active Runs

`Active runs` is the middle column of the Operations section — a list of up to five active runs with device counts and state badges, plus a `View runs` link to `/runs`. It answers whether reservation pressure is expected.

Use it when:

- Devices look reserved and you need quick attribution
- queue pressure may be caused by current CI activity
- you need to jump from fleet view to one specific run

This is a summary only. Use the Runs page for real run management.

## System Health Pills

System-layer status lives in the page header rather than a dedicated row. The header renders three pills — `Stream`, `DB`, and `Grid` — so you can separate system-layer issues from device-layer issues at a glance.

### Stream

The `Stream` pill shows whether live updates are streaming (`Live`) or the page has fallen back to polling (`Polling`).

### DB

The `DB` pill shows whether the backend can reach the database.

If this is down, stop treating device symptoms as isolated fleet issues until the backend is healthy again.

### Grid

The `Grid` pill translates raw Grid status into operator language:

- `Ready`
  - Grid is accepting traffic
- `Starting`
  - Grid is reachable and nodes are still registering
- `Waiting for nodes`
  - Grid is reachable but no nodes are registered yet
- `Idle`
  - Grid is reachable and simply waiting for work
- `Unavailable`
  - Grid could not be reached or returned an error

Check this before blaming a single device for broad Appium-session failures.

### Hosts

Host count is the `Hosts` stat card in the top row. It shows the total host count with an `online/total online` hint; there is no separate offline-host figure on the dashboard.

If several devices appear offline at once, compare that symptom to the Hosts card before digging into each device individually.

## Busy Devices List

The `Busy devices` column in the Operations section lists up to six devices in use, each with a name, platform icon, and an availability badge. Each name links to device detail, and a `View busy` link goes to `/devices?status=busy`. The list includes devices in the `verifying` state in addition to `busy`.

Use it to:

- confirm which devices are in live use
- jump to a busy device quickly
- separate active test traffic from unhealthy idle inventory

## Practical Triage Playbook

### Queue is rising

1. check `Active runs`
2. check the `Grid` pill
3. check whether the Devices card's available count is low or whether nodes are just missing

### Devices are failing one by one

1. check `Device recovery`
2. open recent incidents
3. drill into one representative Device Detail

### Many devices go offline together

1. check the `Hosts` card
2. confirm whether the online/total host count dropped
3. move into Hosts and Host Detail before touching individual devices

## Related Guides

- [Runs And Reservations](runs-and-reservations.md)
- [Hosts And Host Detail Operations](hosts-and-host-detail-operations.md)
- [Lifecycle, Maintenance, And Recovery](lifecycle-maintenance-and-recovery.md)
