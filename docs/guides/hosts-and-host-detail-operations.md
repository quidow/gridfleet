# Hosts And Host Detail Operations

This guide covers the day-to-day host workflows after a host exists in the system: approval, inspection, discovery, driver sync, and deletion.

For initial enrollment and self-registration behavior, start with [Host Onboarding](host-onboarding.md).

## When To Use Hosts vs Host Detail

Use `Hosts` when you need fleet-wide host operations:

- approve or reject pending hosts
- compare heartbeat freshness
- start discovery on one host
- delete an unused host

Use `Host Detail` when you need one host's full operational picture:

- host identity and heartbeat history
- capabilities
- devices currently attached to that host
- per-host Appium driver sync status
- host-scoped discovery and import flow

## Host States

The current operator-facing host states are:

- `pending`
  - the host exists but still needs approval
- `online`
  - the host is accepted and recently reachable
- `offline`
  - the host exists but heartbeat checks currently consider it unreachable

`pending` hosts offer approve/reject actions. Accepted hosts offer discovery and, from Host Detail, driver sync.

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

Host Detail is the operational checklist for one machine.

Main sections:

- `Host Info`
  - IP, OS, agent port, status, version, heartbeat, created time
- `Actions`
  - approve/reject if pending, or `Discover Devices` if accepted
- `Capabilities`
  - platform and tool summary reported by the agent
- `Diagnostics`
  - per-host circuit-breaker state, latest managed Appium processes snapshot, and recent agent-local Appium recovery events
- `Devices`
  - current attached device records for that host
- `Tool Versions`
  - Appium, Node provider, Node, and iOS helper versions plus a background `Ensure Versions` job
- `Appium Drivers`
  - required versus installed driver versions plus `Sync Drivers`

Use this page before large discovery/import work, after agent upgrades, or when many devices on one host start failing together.

## Diagnostics

Host Detail now includes a dedicated `Diagnostics` section backed by `GET /api/hosts/{host_id}/diagnostics`.

Use it when you need to answer:

- is the shared backend agent circuit currently open for this host
- when did this host last report managed Appium process state
- which Appium ports are currently reported as running, including unmapped process ports
- whether the agent recently detected a local Appium crash, recovered it, or exhausted local restart attempts

This surface is diagnostic only. It does not add manual diagnostics controls.

## Agent Version Notices

Host Detail and Hosts both surface version trust.

Current outcomes:

- normal version
  - no warning
- `Outdated`
  - host version is below `agent.min_version`
- `Unknown`
  - the host reported a version the manager could not parse against the configured minimum

An outdated or unknown host can still appear operational, but operator trust should be lower until the agent is updated or corrected.

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

## Driver Sync

Host Detail shows the current required-versus-installed driver picture for that host.

Use per-host `Sync Drivers` when:

- one host missed a previous rollout
- capabilities changed on one machine
- troubleshooting shows mismatched or missing drivers only on that host

Use driver pack reconciliation from Settings when installed host runtimes should converge with the active pack catalog.

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
3. wait for discovery and driver sync, or run discovery manually

### One host looks unhealthy

1. compare `Last Heartbeat`
2. check version warning and capabilities
3. confirm whether the issue is host-wide by reviewing the host's device list
4. run discovery only after agent reachability is restored

### You just updated driver definitions

1. open the affected host in Host Detail
2. use `Sync Drivers` to reconcile that host's Appium driver runtime with the current catalog

## Related Guides

- [Host Onboarding](host-onboarding.md)
- [Device Intake And Discovery](device-intake-and-discovery.md)
- [Settings And Operational Controls](settings-and-operational-controls.md)
