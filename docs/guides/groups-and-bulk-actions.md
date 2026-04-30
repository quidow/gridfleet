# Groups And Bulk Actions

This guide explains how to operate on many devices at once, when to use a saved group versus a temporary selection, and which actions are available on each surface.

## Three Ways To Target Multiple Devices

| Surface | Best For | Membership Model |
| --- | --- | --- |
| ad hoc bulk selection | one-off fleet work right now | current manual selection only |
| static device group | a named hand-curated set | members are added and removed manually |
| dynamic device group | a living rule-based set | members are derived from saved filters |

Use a temporary bulk selection when the action is urgent and unlikely to be repeated. Use a group when the same fleet slice matters over time.

## Device Groups Page

The `Device Groups` page is where groups are created and listed.

Operators can:

- create a group
- choose `static` or `dynamic`
- add a description
- define filters for dynamic groups
- open a group's detail page
- delete a group

Dynamic groups use the same backend-owned contract as `GET /api/devices`, except that free-text `search` is not persisted as part of the group definition.

Current dynamic-group filter surface:

- platform
- status
- host
- identity value
- connection target
- device type
- connection type
- OS version
- lifecycle state
- exact-match tags

Static groups are just named member lists.

## Group Detail Workflow

Group Detail gives you two different control layers:

- the whole-group action bar
- the member table

Current shipped behaviors:

- static groups can add or remove members manually
- dynamic groups can edit filters but cannot add or remove members manually
- static group member rows support selection checkboxes for subset-only bulk actions
- dynamic group detail still supports whole-group actions against the currently matched members

## Action Surface Matrix

| Action | Bulk Selection Toolbar | Whole Group Action Bar | Notes |
| --- | --- | --- | --- |
| Start nodes | Yes | Yes | per-device blockers still apply |
| Stop nodes | Yes | Yes | reserved devices fail individually |
| Restart nodes | Yes | Yes | blocked by reservation, maintenance, or readiness per device |
| Reconnect | Yes | Yes | only works for eligible network Android / Fire TV devices |
| Enter maintenance | Yes | Yes | current UI uses immediate maintenance, not drain |
| Exit maintenance | Yes | Yes | device must already be in maintenance |
| Apply template | Yes | Yes | template picker requires one shared platform |
| Update tags | Yes | Yes | merge or replace behavior is supported |
| Delete devices | Yes | Yes | deletes device records, not just group membership |
| Auto-manage toggle | Yes | No | currently available only from the ad hoc bulk toolbar |

That last row is important: whole-group actions are broad, but they are not perfectly identical to the temporary bulk toolbar.

## Bulk Selection Workflow

The temporary bulk toolbar is best when:

- you filtered Devices down to a short-lived target set
- you want to act on only some members of a static group
- you need the `Auto-Manage` toggle, which the whole-group bar does not currently expose

Bulk actions report success and failure per device. If some devices fail, the UI opens an error dialog rather than hiding partial failure.

## Group Types In Practice

### Static Groups

Use static groups when:

- membership is operator-owned
- the same devices should stay together even if tags or status change
- you want both whole-group actions and subset selection from the member table

### Dynamic Groups

Use dynamic groups when:

- the target set is defined by fleet traits such as platform, host, lifecycle state, status, or tags
- membership should update automatically as the fleet changes
- you want an operational cohort without maintaining member lists manually

Current limitation:

- dynamic groups cannot manually add or remove members

## Action Blockers And Eligibility

Bulk and group actions still respect device-level rules.

Common blockers:

- reserved devices block direct node control
- maintenance blocks start and restart
- readiness gates block start and restart for unverified devices
- reconnect only works for supported network Android / Fire TV devices with host and IP data

The action surface is broad, but it is intentionally not a bypass around reservation, maintenance, or readiness rules.

## Templates And Tags

`Apply Template` and `Update Tags` are configuration-shaping tools, not transport controls.

Shipped behavior:

- template apply can merge or replace
- tag update can merge or replace
- template apply requires a single platform across the current target set so the template picker stays valid

Use these actions when standardizing a cohort before verification, maintenance, or repeated CI use.

## Maintenance Flows

Group and bulk maintenance are for operator-owned holds across multiple devices.

Current UI behavior:

- entering maintenance uses immediate maintenance (`drain=false`)
- exiting maintenance returns those devices out of the operator hold, but does not itself verify or restart them

If the devices also have readiness or lifecycle blockers, clear those separately before expecting them to return to service.

## Practical Operator Playbook

### Same action on a temporary filtered set

- use bulk selection from Devices

### Same action on a named long-lived cohort

- create a group and use whole-group actions

### Same action on only part of a static group

- select specific members inside Group Detail and use the bulk toolbar

## Related Guides

- [Lifecycle, Maintenance, And Recovery](lifecycle-maintenance-and-recovery.md)
- [Verification And Readiness](verification-and-readiness.md)
- [Hosts And Host Detail Operations](hosts-and-host-detail-operations.md)
