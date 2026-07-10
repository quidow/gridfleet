# Host Onboarding

This guide covers how a host becomes usable in GridFleet, how self-registration behaves, and what to check before you start discovery or intake work.

## Before You Start

Make sure the machine already meets the prerequisites in [Host Requirements](host-requirements.md) before you install or start the agent. Host onboarding assumes the OS-specific tooling is already present.

## What A Host Is

A host is the machine that can actually see and control devices. The manager never treats devices as standalone inventory anymore. Every supported device belongs to a host.

Typical examples:

- a Linux host for Android and Fire TV devices
- a macOS host for iOS, tvOS, simulators, and any Android devices physically attached there

## Supported Onboarding Paths

### Preferred: Agent Self-Registration

The agent starts a background registration loop on startup and posts itself to the manager.
After a successful registration, it keeps refreshing the registration periodically so enrollment details such as the
advertised IP address can recover after a network change without restarting the agent. Registration is enrollment only —
mutable runtime facts (agent version, capabilities, missing prerequisites) arrive on the separate status-push channel, not
at registration.

What the agent sends at registration:

- hostname
- reachable IP address
- OS type
- agent port
- hardware descriptor (`host_info`)
- detected capabilities (used only as the orchestration-contract gate credential; not persisted at registration)

What happens next:

- if `agent.auto_accept_hosts` is enabled, the new host is enrolled `online` and reads online immediately via the `created_at` recency grace until the first status push takes over
- if `agent.auto_accept_hosts` is disabled, the new host is created as `pending`
- successful accept also triggers background device discovery

Use this path when the host is already running the installed agent from the deployment flow in [deployment.md](deployment.md).

### Manual: Add Host In The UI

Operators can also add a host manually from the Hosts page.

Required fields:

- hostname
- IP address
- OS type

Agent port is optional. When omitted it defaults to the `agent.default_port` setting (the Add Host form pre-fills 5100).

Manual add is useful when:

- you want the host visible before the agent registers
- you are validating network reachability or operator-owned metadata first
- you are working around a temporary self-registration issue

## Approval Flow

Hosts can appear in one of three states:

- `pending`: waiting for operator approval
- `online`: accepted and recently reachable
- `offline`: accepted before, but heartbeat checks are currently failing

If a host is `pending`, approve or reject it from:

- the Hosts table
- the Host Detail page

Approval behavior:

- sets the host to `online`
- triggers automatic discovery

Driver packs are delivered separately by the pack desired-state pipeline that the agent pulls, not by approval. Appium itself ships inside each driver-pack runtime and is not version-ensured on approval.

Rejection behavior:

- removes the pending host record entirely

## First Operator Checks After A Host Appears

Open the host in Hosts or Host Detail and confirm:

- the host is `online`
- the reported IP and OS are correct
- the reported agent version is not marked `outdated`
- the capabilities block matches what the machine should support
- the Host Tools card shows the expected Node and Node Provider versions (iOS-helper and other driver/platform tool versions, when present, appear separately under the Driver Pack Dependencies card, derived from each driver pack's tool dependencies)
- the Appium Drivers section is present and can be synced

The Host Detail page is the best place to confirm all of that before importing devices.

## Discovery Starts From Hosts

Discovery is intentionally host-scoped.

Run it from:

- the search action on the Hosts page
- the `Discover Devices` button on Host Detail

That query only checks devices visible to that specific host agent. Device intake and re-verification rely on that host-scoped visibility model.

## Common Outcomes

### Auto-Accept Enabled

Expected flow:

1. Install and start the agent.
2. The agent registers itself.
3. The host appears as `online`.
4. Automatic discovery begins.

### Auto-Accept Disabled

Expected flow:

1. Install and start the agent.
2. The host registers itself.
3. The host appears as `pending`.
4. An operator approves it.
5. Discovery begins after approval.

## Troubleshooting

### Host stays pending

Most likely cause: `agent.auto_accept_hosts` is disabled. Approve the host from Hosts or Host Detail.

### Host shows outdated or unknown agent version

The host is still usable, but operator trust should be lower until the agent is updated or reporting correctly. Host Detail shows the minimum configured version and the current status.

### Discover fails with an agent reachability error

The manager can see the host record, but cannot reach the running agent at the saved host IP and agent port. Confirm:

- the agent process is running
- the agent port is open
- the host IP is reachable from the manager
- the saved IP/port match the host's current advertise settings

### Delete host is blocked

Host deletion is blocked while devices are still assigned to that host. Remove or re-home the devices first.
