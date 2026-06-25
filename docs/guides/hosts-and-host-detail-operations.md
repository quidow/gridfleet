# Hosts And Host Detail Operations

This guide covers the day-to-day host workflows after a host exists in the system: approval, inspection, discovery, driver-pack status review, and deletion.

For initial enrollment and self-registration behavior, start with [Host Onboarding](host-onboarding.md).

## When To Use Hosts vs Host Detail

Use `Hosts` when you need fleet-wide host operations:

- approve or reject pending hosts
- compare heartbeat freshness
- start discovery on one host
- delete an unused host

Use `Host Detail` when you need one host's full operational picture:

- host identity and heartbeat history
- live host tool versions and driver-pack dependencies
- devices currently attached to that host
- per-host driver-pack status, including installed-versus-desired Appium driver versions
- host-scoped discovery and import flow

## Host States

The current operator-facing host states are:

- `pending`
  - the host exists but still needs approval
- `online`
  - the host is accepted and recently reachable
- `offline`
  - the host exists but heartbeat checks currently consider it unreachable

`pending` hosts offer approve/reject actions. Accepted hosts offer discovery and, from Host Detail, per-pack driver doctor checks.

## Hosts Page Workflow

The Hosts table is the quick-action view.

It supports:

- sorting by hostname, IP, OS, status, agent version, device count, and last heartbeat
- `Add Host`
- approve or reject for pending hosts
- `Discover Devices` for accepted hosts
- `Delete Host`

The row-level discovery action is useful when you want a fast diff without leaving the list.

## Host Detail Workflow

Host Detail is the operational checklist for one machine. It is a tabbed page.

Tabs:

- `Overview`
  - `Host Info` (IP, OS, agent port, status, version, heartbeat, created time), with an agent-version notice when the version is below the configured minimum or recommended version
  - the resource strip and circuit-breaker card
  - the `Host Tools` panel (Node and Node Provider) and, when at least one desired pack declares them, the `Driver Pack Dependencies` panel
  - resource telemetry
  - an `Actions` card (Approve/Reject) that appears only while the host is pending
- `Devices`
  - current attached device records for that host, with a `Discover Devices` button in the tab header
- `Drivers`
  - per-pack driver status (see [Driver Status](#driver-status))
- `Environment`
  - per-host tool environment variables
- `Agent Logs`
  - recent agent-local log output
- `Events`
  - recent host events

Legacy `?tab=diagnostics` redirects to `Overview` and `?tab=logs` redirects to `Agent Logs`.

Use this page before large discovery/import work, after agent upgrades, or when many devices on one host start failing together.

## Diagnostics

The `GET /api/hosts/{host_id}/diagnostics` payload is consumed across two tabs rather than a single dedicated section:

- the `Overview` tab renders the circuit-breaker card from `circuit_breaker`
- the `Devices` tab uses the managed Appium process snapshot (`appium_processes.running_nodes`) to show node state, including unmapped process ports

Use these surfaces when you need to answer:

- is the shared backend agent circuit currently open for this host
- when did this host last report managed Appium process state
- which Appium ports are currently reported as running, including unmapped process ports

These surfaces are diagnostic only. They do not add manual diagnostics controls.

## Agent Version Notices

Host Detail and Hosts both surface version trust.

Current outcomes:

- normal version
  - no warning
- `Outdated`
  - host version is below `agent.min_version`
- `Update available`
  - host version is at or above `agent.min_version` but below the configured `agent.recommended_version` (the backend sets `agent_update_available`); it renders as an `Update available` table badge and an `Agent update available` detail notice
- `Unknown`
  - the host reported a version the manager could not parse against the configured minimum

An outdated, update-available, or unknown host can still appear operational, but operator trust should be lower until the agent is updated or corrected.

## Discovery From Host Surfaces

Discovery is always host-scoped.

From Hosts or Host Detail, `Discover Devices` shows:

- new devices
- updated devices
- removed identities

Operators can:

- bulk-import selected new devices
- opt into removal of missing identities
- use the one-click `Import & Verify` or `Import & Complete Setup` path for a new device

For the device-side meaning of those discovery results, continue to [Device Intake And Discovery](device-intake-and-discovery.md).

## Driver Status

The `Drivers` tab shows a per-pack table for that host: pack status and release, the installed-versus-desired Appium driver version (with a `wanted: <version>` drift indicator when they differ), the isolated runtime, and per-pack and per-feature health.

Per-pack and per-feature actions:

- `Run Doctor` (`POST /api/hosts/{host_id}/driver-packs/{pack_id}/doctor`) re-runs that pack's doctor checks and expands the per-check results
- per-feature action buttons run the feature actions declared in the pack manifest

There is no per-host driver-sync action. Driver runtime convergence is driven by driver-pack reconciliation from Settings, which is how installed host runtimes converge with the active pack catalog.

## Deleting A Host

Delete host is intentionally conservative.

Current shipped rule:

- deletion is blocked while devices are still assigned to that host

That means host cleanup is usually:

1. remove or re-home the host's devices
2. confirm the host is no longer needed
3. delete the empty host record

## Practical Operator Playbook

### A new pending host appears

1. review IP, OS, and version
2. approve it if expected
3. wait for discovery and driver-pack reconciliation, or run discovery manually

### One host looks unhealthy

1. compare `Last Heartbeat`
2. check the version notice and the `Host Tools` panel on the Overview tab
3. confirm whether the issue is host-wide by reviewing the host's device list
4. run discovery only after agent reachability is restored

### You just updated driver definitions

1. open the affected host in Host Detail
2. open the `Drivers` tab to review per-pack status and installed-versus-desired Appium driver versions, and use `Run Doctor` on a pack to re-check its health; runtime convergence is driven by driver-pack reconciliation from Settings

## Related Guides

- [Host Onboarding](host-onboarding.md)
- [Device Intake And Discovery](device-intake-and-discovery.md)
- [Settings And Operational Controls](settings-and-operational-controls.md)
