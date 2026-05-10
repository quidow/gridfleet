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
After a successful registration, it keeps refreshing the registration periodically so mutable host details such as the
advertised IP address can recover after a network change without restarting the agent.

What the agent sends:

- hostname
- reachable IP address
- OS type
- agent port
- agent version
- detected capabilities

What happens next:

- if `agent.auto_accept_hosts` is enabled, the new host is created as `online`
- if `agent.auto_accept_hosts` is disabled, the new host is created as `pending`
- successful accept also triggers background device discovery and driver sync

Use this path when the host is already running the installed agent from the deployment flow in [deployment.md](deployment.md).

### Manual: Add Host In The UI

Operators can also add a host manually from the Hosts page.

Required fields:

- hostname
- IP address
- OS type
- agent port

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
- ensures the configured Appium version
- triggers automatic Appium driver sync after tool version checks

Rejection behavior:

- removes the pending host record entirely

## First Operator Checks After A Host Appears

Open the host in Hosts or Host Detail and confirm:

- the host is `online`
- the reported IP and OS are correct
- the reported agent version is not marked `outdated`
- the capabilities block matches what the machine should support
- the Tool Versions section shows the expected Appium, Node, Node provider, and iOS helper versions
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
4. Automatic discovery and driver sync begin.

### Auto-Accept Disabled

Expected flow:

1. Install and start the agent.
2. The host registers itself.
3. The host appears as `pending`.
4. An operator approves it.
5. Discovery and driver sync begin after approval.

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
