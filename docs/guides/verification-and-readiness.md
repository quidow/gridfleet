# Verification And Readiness

This guide explains what the readiness badge means, when verification runs, what each verification stage does, and how success or failure affects the saved device.

## Readiness States

Every device has one readiness state:

| State | Meaning | Operator Action |
| --- | --- | --- |
| `Setup Required` | Required operator-supplied setup is still missing | Complete the missing fields, then verify |
| `Needs Verification` | Setup is present, but the current saved configuration has not been verified | Run guided verification |
| `Verified` | The current saved configuration passed the verification flow | Device can participate in normal node and reservation workflows |

Readiness is separate from availability:

- readiness answers "is this configuration safe to use?"
- status answers "what is the device doing right now?"

## Current Missing-Setup Rules

There are two distinct, intentionally narrow rules. Do not conflate them.

A device stays in `Setup Required` (and verification will not start) when a pack device-field marked `required_for_session` is empty — for example the Roku developer password, the tvOS WDA base URL (`appium:wdaBaseUrl`), or the tvOS updated WDA bundle ID. This state is derived solely from the active pack's required device fields.

The IP-address requirement is a separate pack-level rule (`connection_behavior.requires_ip_address`) enforced at save/validation time, not a `Setup Required` trigger. It is currently `true` only for the Roku network platform. The Android and Fire TV network platforms set `requires_ip_address: false`, so they do not require an IP address to save. Virtual devices never require IP-based setup.

## Where Verification Starts

Verification can begin from several operator surfaces:

- `Verify & Add Device` in Add Device
- `Import & Verify` or `Import & Complete Setup` after discovery
- `Verify` / `Re-verify` from Device Detail
- guided re-verification after readiness-impacting edits

Readiness-impacting edit paths currently include saved changes to the connection target, the IP address, or any pack-defined device-config field — for example the Roku developer password and the tvOS WDA base URL (`appium:wdaBaseUrl`), preinstalled-WDA choice, and updated WDA bundle ID.

Those edits do not silently keep the old verified state. The UI hands you into guided verification instead.

## Verification Stages

Every verification job uses the same six-stage progress model:

| Stage | What It Does |
| --- | --- |
| `Validate Input` | Normalizes the payload, resolves host/diagnostics-derived values, and blocks missing setup |
| `Check Device Health` | Asks the selected host agent whether the device target is actually reachable and healthy |
| `Start Appium Node` | Starts a temporary Appium node for probing |
| `Probe Appium Session` | Creates and tears down a real Appium session directly against the device's Appium server |
| `Clean Up Probe` | Stops the temporary node, or retains it as the managed node when allowed |
| `Save Device` | Persists the verified create or verified update |

The progress panel keeps stage-by-stage detail so operators can retry from the same form instead of guessing which part failed.

## What Success Means

### New Device

On create, the device is not saved unless verification reaches the final save stage successfully.

Success means:

- the normalized identity and connection target are accepted
- health and probe succeeded
- the device is saved as verified

### Existing Device

On re-verification, the existing device keeps its current saved state unless the verification-backed update completes successfully.

Success means:

- the updated payload passed validation
- setup requirements were satisfied
- health and probe succeeded
- the verified update was committed

## Node Retention After Successful Verification

Verification uses a temporary Appium node for the probe. After a successful verification, the verified node is retained as the managed Appium node, leaving the device immediately ready for normal node-backed work.

## Failure Behavior

Failure is stage-specific and intentionally conservative.

Examples:

- validation failure means the job never reaches health or probe
- health failure means no session probe is attempted
- probe failure still performs cleanup before the job ends
- save failure does not silently keep a partial create/update

For operators, the important rule is simple:

- failed create verification does not save a new device
- failed re-verification does not apply the new verified configuration

## Verification And Platform-Specific Setup

### Android Network

Verification must resolve the device to a stable ADB serial on the selected host before the device can be saved.

### Virtual Devices

Emulators and simulators now persist with `connection_type=virtual`.

That means:

- they do not borrow USB or network labels in the UI
- they do not require IP input for readiness
- verification still uses the selected host to confirm the device target is healthy and probe-able

### Roku

Verification depends on the Roku developer password being present. Without it, the device stays in `Setup Required`.

### tvOS Real Devices

The setup modal collects the required WDA endpoint and preinstalled-WDA settings:

- `appium:usePreinstalledWDA`
- `appium:updatedWDABundleId`
- `appium:wdaBaseUrl`

`appium:wdaBaseUrl` is required for real tvOS setup so the session can attach to the Apple TV's preinstalled WDA endpoint.

## Common Operator Decisions

### When to re-verify proactively

Use `Re-verify` when:

- a active target changed
- setup/config changed in a readiness-impacting way
- you want a fresh probe before returning a device to service

### When not to treat a device as usable

Do not treat the device as ready for node start or reservations when the readiness badge shows:

- `Setup Required`
- `Needs Verification`

The UI and API both enforce those gates.

## Troubleshooting

### Validation fails immediately

Look for missing setup or unresolved host/diagnostics-derived values. The most common cases are missing IP/password data or Android network resolution failures.

### Health check fails

The host can see the device record, but the device target is not healthy enough to probe right now. Check the host-visible transport first.

### Session probe fails

The device passed health but could not sustain a real direct-to-Appium session. Use the stage detail and Device Health panel together.

### Verification succeeded, but there is no running node

Check that node start was not blocked by a maintenance hold or a pending readiness state. Verification cleans up the temporary probe node after success, but the managed node should be started automatically.
