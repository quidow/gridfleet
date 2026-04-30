# Device Intake And Discovery

This guide explains the host-first intake model: where discovery runs, how discovered results differ from intake candidates, and what operators need to enter manually for each lane.

## Three Related Concepts

### Discovery Results

Discovery is a host-scoped diff against what the selected host agent can currently see.

The discovery modal can show:

- `New Devices`: visible on the host but not yet in the registry
- `Updated Devices`: already registered devices whose host-observed properties changed
- `Removed Devices`: registered identities no longer visible to that host

Discovery is started only from Hosts or Host Detail.

### Intake Candidates

Intake candidates are the host-visible devices shown in the Add Device modal after you choose a host and lane.

They are used to:

- prefill stable identity when the host already sees the device
- prefill connection target and OS version
- prevent choosing devices that are already registered

The Add Device modal treats this candidate list as live observed data. It shows an observed-device section with a live/count indicator and update timestamp so operators can tell when the host-visible list changed while the modal is open.

### Manual Fields

Manual fields are only used when the host cannot provide everything automatically.

Current examples:

- Android network lane can accept a manual `IP:port` target
- Roku intake requires an IP address and developer password
- display name can always be overridden

Operators do not enter identity values in Add Device. Identity is observed from the selected host-visible device or resolved during verification.
For registered devices, IP address and connection target changes are operator-owned setup changes and must go through guided verification before the device is trusted again.

## Discovery Workflow

Use discovery when you want to reconcile a host's visible hardware with the manager's saved registry.

Typical flow:

1. Open Hosts or Host Detail.
2. Run `Discover Devices`.
3. Review new, updated, and removed identities.
4. Choose what to import and what to remove.
5. Apply the diff, or use the one-click import-and-verify action for a single new device.

Important shipped behavior:

- updated devices are informational; confirm only auto-applies version facts such as OS/software versions
- new devices can be imported in bulk
- removed devices are opt-in deletes
- import creates a host-backed device record immediately, but it may still need setup or verification before use

## Add Device Workflow

The Add Device modal is a host-first verification flow, not a raw create form.

What always happens first:

1. select a host
2. choose driver platform, device type, and connection lane
3. choose a host-visible device when that lane requires one
4. verify before the device is saved

The modal shows derived device data for:

- stable identity
- connection target
- OS version

For several lanes, those values stay unresolved until verification runs.

## Lane Matrix

| Lane | Host Required | Candidate Required | Manual Inputs |
| --- | --- | --- | --- |
| Android mobile real device over USB | Yes | Yes | Optional display name |
| Android TV real device over USB | Yes | Yes | Optional display name |
| Fire TV real device over USB/network as seen by host | Yes | Yes for host-visible device lanes | Optional display name |
| Android real device over network | Yes | Optional | `IP:port` allowed if no candidate is selected |
| Android emulator (`Virtual`) | Yes | Yes | Optional display name |
| iOS real device | Yes | Yes | Optional display name |
| iOS simulator (`Virtual`) | Yes | Yes | Optional display name |
| tvOS real device | Yes | Yes | Optional display name |
| tvOS simulator (`Virtual`) | Yes | Yes | Optional display name |
| Roku | Yes | No | Roku IP address, Roku developer password, optional display name |

## Platform Notes

### Android USB

Expected path:

- select a host
- pick the discovered device
- verify and save

The stable identity comes from the host-visible ADB serial.

### Android Network

This is the special lane.

You can:

- choose a discovered device if the host already sees it
- or type a manual `IP:port` target

The device still cannot be saved until verification resolves that transport target to a stable ADB serial on the selected host.

### iOS And tvOS

These lanes are device-picker-first.

Operators normally choose the host-visible device rather than typing low-level identifiers manually.

### Virtual Devices (Emulators & Simulators)

Virtual devices have unique explicit controls established during the intake and discovery configuration:

- **Virtual connection lanes:** Emulators and simulators uniformly persist with their connection types constrained, requiring no manual IP input.
- **Headless Mode Toggles:** Available for Android Emulators (AVDs), allowing the operator to instruct the agent to skip display rendering for faster setup and lower host load.
- **Auto-Boot Lifecycle:** Virtual devices natively expose their process state (e.g., `stopped`, `booting`, `running`). A `stopped` virtual device can be safely targeted; the agent is responsible for automatically launching the hardware artifact seamlessly before starting Appium.

### Roku

Roku is still host-first, but not candidate-first.

Operators provide:

- host
- Roku IP address
- Roku developer password

Verification can then enrich the host-derived identity and metadata through the selected host.

## Identity vs Connection Target

The UI distinguishes two different values:

- `Identity`: the stable identifier used to recognize the same device over time
- `Connection Target`: the current route the manager uses right now

Examples:

- Android network device: identity becomes a stable ADB serial, while the connection target may stay an `IP:port`
- Roku: the connection target is the Roku IP, while the stored identity may be derived from host lookup

Operators should treat identity as the durable record key and connection target as the device route.

## Import From Discovery vs Add Device

Use discovery import when:

- the host already sees the device
- you want to reconcile multiple devices at once
- you want to turn a discovered device into a registry record first and finish setup immediately after

Use Add Device when:

- you are onboarding a single device intentionally
- you want lane-specific host-first verification before first save
- you need a manual Android network target or Roku credentials

## Troubleshooting

### Candidate is disabled as already registered

That device target is already matched to an existing device record. Open the existing device instead of adding a duplicate.

### Discovery shows the device, but Add Device cannot save it

The most common reason is verification failure, not discovery failure. Open the verification progress panel and use the failed stage detail.

### Android network lane stays unresolved

The selected host must be able to reconnect to the `IP:port` target and resolve it to a stable ADB serial. If resolution fails, the device is not saved.

### Imported device still is not usable

Import only creates the host-backed record. If the device lands in `Setup Required` or `Needs Verification`, complete setup and run verification before trying to start nodes or reserve it.
