# Groups And Bulk Actions

This guide explains how to operate on many devices at once, when to use a saved group versus a temporary selection, and which actions are available on each surface.

## Three Ways To Target Multiple Devices

| Surface | Best For | Membership Model |
| --- | --- | --- |
| ad hoc bulk selection | one-off fleet work right now | current manual selection only |
| static device group | a named hand-curated set | members are added and removed manually |
| dynamic device group | a living rule-based set | members are derived from saved filters |

Use a temporary bulk selection when the action is urgent and unlikely to be repeated. Use a group when the same fleet slice matters over time.

Groups are also the routing primitive: a test client asks for "any device in this group" through a W3C capability, and a run reserves against group membership. See [Routing Sessions To A Group](#routing-sessions-to-a-group).

## Group Keys

Every group has a `key` — the short, public, URL-safe identifier used by routes, capabilities, run requirements, events, and export bundles.

- format: `^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$` — lowercase letters, digits, and inner hyphens, 1–64 characters, no leading or trailing hyphen
- keys are **immutable**: `PATCH /api/device-groups/{group_key}` accepts `name`, `description`, and `filters` only. To change a key, create a new group and move the members
- keys are unique across the fleet, regardless of group type
- `name` and `description` are free-text display fields and can be edited at any time

The group's internal database id is a UUID and is never part of a public route, schema, event payload, or client call. Address groups by key everywhere.

```
/api/device-groups/east-lab           # correct
/api/device-groups/{uuid}             # not a supported address
```

## Device Groups Page

The `Device Groups` page is where groups are created and listed.

Operators can:

- create a group with a key, a name, and a type
- choose `static` or `dynamic`
- add a description
- define filters for dynamic groups
- open a group's detail page
- delete a group

Dynamic groups use the same backend-owned contract as `GET /api/devices`, except that free-text `search` is not persisted as part of the group definition.

Current dynamic-group filter surface:

- driver pack (`pack_id`)
- platform (`platform_id`)
- status
- reserved
- host (`host_id`)
- identity value
- connection target
- device type
- connection type
- OS version and OS version display
- hardware health status
- hardware telemetry state
- needs attention
- `member_of` — a list of **static** group keys

Every axis a dynamic group pins is ANDed. `member_of` is ANDed with the rest: a device belongs to the dynamic group only if it matches every pinned filter *and* is a member of every static group named in `member_of`.

`member_of` may reference static groups only. The backend validates the reference on create and update and rejects a dynamic or unknown key. Deleting a static group that a dynamic group still references is rejected until the reference is removed.

Static groups are named member lists and cannot define filters.

Dynamic membership is evaluated live from current fleet facts on every read, allocation poll, and bulk action. It is never materialized into a membership table and never cached, so a fleet change is visible to the very next evaluation — there is no refresh step and no staleness window to wait out.

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
| Enter maintenance | Yes | Yes | |
| Exit maintenance | Yes | Yes | device must already be in maintenance |
| Delete devices | Yes | Yes | deletes device records, not just group membership |

Both surfaces expose the same actions; they differ only in scope (the bulk toolbar acts on the current selection, the whole-group bar acts on every current group member).

## Bulk Selection Workflow

The temporary bulk toolbar is best when:

- you filtered Devices down to a short-lived target set
- you want to act on only some members of a static group

Bulk actions report success and failure per device. If some devices fail, the UI opens an error dialog rather than hiding partial failure.

## Group Types In Practice

### Static Groups

Use static groups when:

- membership is operator-owned
- the same devices should stay together even as fleet traits such as status or OS version change
- you want both whole-group actions and subset selection from the member table
- the group is a `member_of` target for a dynamic group

### Dynamic Groups

Use dynamic groups when:

- the target set is defined by fleet traits such as platform, host, or status
- membership should update automatically as the fleet changes
- you want an operational cohort without maintaining member lists manually
- you want to narrow an operator-curated static group by a live trait, via `member_of`

Current limitations:

- dynamic groups cannot manually add or remove members
- `member_of` cannot reference another dynamic group

## Action Blockers And Eligibility

Bulk and group actions still respect device-level rules.

Common blockers:

- reserved devices block direct node control
- maintenance blocks start and restart
- readiness gates block start and restart for unverified devices
- reconnect only works for supported network Android / Fire TV devices with host and IP data

The action surface is broad, but it is intentionally not a bypass around reservation, maintenance, or readiness rules.

## Routing Sessions To A Group

A test client asks the router for a device in a group with a vendor capability:

```json
{"gridfleet:group:east-lab": true}
```

The value must be the JSON boolean `true`. Any other value — `"true"`, `1`, `false`, an object — is rejected.

Both static and dynamic groups route the same way. The client names a key; whether the members come from a curated list or a live filter is a fleet-side decision the client never sees.

### Combining Groups: AND Within A Candidate, OR Across `firstMatch`

The router merges each `firstMatch` entry with `alwaysMatch` to form one candidate, the standard W3C merge. Group capabilities compose along that same structure:

- multiple group capabilities in a **single merged candidate** are **ANDed** — the device must be in every named group
- separate **`firstMatch` candidates** stay **OR** alternatives — the device must satisfy any one of them

Devices in `east-lab` **and** `screen-type-4k`:

```json
{
  "capabilities": {
    "alwaysMatch": {
      "platformName": "Android",
      "gridfleet:group:east-lab": true,
      "gridfleet:group:screen-type-4k": true
    }
  }
}
```

Devices in `east-lab` **or** `west-lab`:

```json
{
  "capabilities": {
    "alwaysMatch": {"platformName": "Android"},
    "firstMatch": [
      {"gridfleet:group:east-lab": true},
      {"gridfleet:group:west-lab": true}
    ]
  }
}
```

### Rejected Selectors

These fail the session request with `400` and cancel the queue ticket rather than silently matching a wider set of devices:

| Selector problem | Example |
| --- | --- |
| key does not satisfy the key format | `gridfleet:group:East_Lab` |
| value is not the boolean `true` | `{"gridfleet:group:east-lab": "true"}` |
| group key does not exist, or was deleted | `gridfleet:group:decommissioned-lab` |
| retired tag capability | `gridfleet:tag:team` |

A valid key that currently matches no free device is **not** an error — the ticket waits in the queue like any other unsatisfiable request until it is filled or times out.

### Allocation Cost

Group routing does not scale with fleet or group count. A free group-routed allocation poll issues a fixed four database reads before a claim, regardless of how many devices, groups, or platforms exist:

1. older waiting tickets' candidate sets (the FIFO veto)
2. the referenced group definitions plus their `member_of` static targets, folded into one recursive CTE
3. eligible devices joined with their per-device group and reservation facts
4. the batched pack-template load

A poll with no group selector skips read 2 and costs three. Binding the ticket to a run adds one scalar run-state read. A successful claim adds a joined `FOR UPDATE` lock read and a live-session recheck before the session is written.

## Targeting Groups Outside The Router

### Runs

A run requirement takes a `groups` list of keys, ANDed with each other and with the requirement's other fields:

```json
{
  "requirements": [
    {"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 2,
     "groups": ["east-lab", "screen-type-4k"]}
  ]
}
```

The run reserves only devices that are in every listed group.

### Device Queries

`GET /api/devices` takes a repeated `group` query parameter. Repeats are ANDed:

```
GET /api/devices?group=east-lab&group=screen-type-4k
```

## Maintenance Flows

Group and bulk maintenance are for operator-owned holds across multiple devices.

Current UI behavior:

- entering maintenance takes effect immediately
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
