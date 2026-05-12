# Host Requirements

Use this guide before installing the host agent.

It answers four questions:

- which platform lanes a host OS can run
- which tools must already exist on the machine
- which process artifacts the agent can manage after the host is accepted
- how to verify the machine is ready before discovery or device intake

## Host Support Matrix

| Host OS | Supported lanes | Not supported |
| --- | --- | --- |
| Linux | Android mobile, Android TV, Fire TV, Roku | iOS, tvOS, Apple simulators |
| macOS | Android mobile, Android TV, Fire TV, Roku, iOS, tvOS, Apple simulators | Windows-only or Linux-only host tooling |

The manager is host-first. A device is only usable when the host can actually see the transport and has the required local tooling.

## What The Agent Manages After Approval

These are the parts the agent can install, verify, or keep aligned after the host is accepted.

| Artifact | How it is managed | Manager setting |
| --- | --- | --- |
| Appium runtime | installed per driver pack under `AGENT_RUNTIME_ROOT` | driver pack catalog |
| Appium driver packs | reconciled from the active pack catalog and checked with Appium doctor | driver pack catalog |
| Appium plugins | installed or removed to match the registry | plugin registry |
| Appium default plugin activation | passed to managed Appium nodes as `--use-plugins` | `appium.default_plugins` |

What the agent does not install for you:

- Node.js or npm
- Android SDK platform tools
- Xcode
- Android SDK system images or AVD creation

The host must already have those prerequisites in place.

## Linux Host Requirements

Linux hosts are for Android and Roku lanes only.

### Required Before Agent Install

- **Node.js and npm**
  - Required so the agent can install per-pack Appium runtimes and drivers.
  - The agent can detect Node from fnm, nvm, standard macOS paths, or a system install, but it does not install Node itself.
- **Android SDK**
  - Required for Android mobile, Android TV, Fire TV, and Android emulator lanes.
  - The minimum useful pieces are the SDK root plus `platform-tools` so `adb` is available.
  - If you plan to use Android emulators, the SDK also needs the `emulator` binary and the relevant system images and AVDs created ahead of time.
- **USB permissions for physical Android devices**
  - Physical Android and Fire TV devices still need normal host-side ADB trust and udev/permission setup.

### Notes

- Linux hosts do not support iOS or tvOS automation.
- The manager filters Apple-only Appium drivers out of Linux host sync and driver-status views.
- Roku does not need extra host binaries beyond the shared process tooling. It uses the network route and ECP checks.

## macOS Host Requirements

macOS hosts can run both Apple and Android lanes.

### Required Before Agent Install

- **Node.js and npm**
  - Required so the agent can install per-pack Appium runtimes and drivers.
  - fnm, nvm, standard macOS paths, and system Node are all supported discovery paths.
- **Android SDK**
  - Required for Android mobile, Android TV, Fire TV, and Android emulator lanes.
  - As on Linux, emulator use also requires the `emulator` binary plus the AVDs and system images you want to run.
- **Xcode with active developer tools**
  - Required for iOS, tvOS, and Apple simulator discovery and automation.
  - `xcodebuild` and `xcrun simctl` must be usable from the shell the agent service inherits.

### Apple Real-Device Notes

- Physical iOS and tvOS devices must still trust the Mac and allow developer tooling access.
- `xcrun devicectl` is used for Apple real-device discovery and health checks.
- `go-ios` provides the `ios` CLI used for iOS real-device battery telemetry.
- Install `go-ios` with npm on hosts that run iOS real-device lanes: `npm install -g go-ios`.

## Emulator And Simulator Preparation

GridFleet can discover existing emulators and simulators, and it can auto-launch an existing Android AVD during verification or managed Appium startup.

What still needs to exist before that works:

- Android SDK with `emulator`
- the target Android system image already installed
- the AVD already created on the host
- Xcode simulator devices already installed for Apple simulator lanes

GridFleet does not currently provision SDK system images, create AVDs, or install new Apple simulator devices from the dashboard.

## Appium Drivers And Plugins

Driver and plugin management is split on purpose:

- **Drivers**
  - Synced from the manager registry through the host agent.
  - Driver sync runs Appium doctor afterward and stores the latest result on the host.
  - Some drivers do not expose doctor checks; the host UI shows that as not applicable instead of as a failure.
- **Plugins**
  - Installed or removed to match the manager registry.
  - Plugin activation is separate and controlled by `appium.default_plugins`.
  - Installing or uninstalling a plugin does not restart already-running Appium nodes.

## Missing-Prerequisite Warnings

Hosts report `missing_prerequisites` from registration and periodic capability refresh.

Those warnings are shown in the host surfaces so operators can distinguish:

- a host that is enrolled but cannot yet run a lane
- a host that still needs manual setup

Current remediation behavior:

- Appium runtimes are reconciled by the desired driver-pack loop.
- Missing host-level tools such as `go_ios`, `java`, `adb`, Xcode, or the Android SDK are informational and require operator setup.

## Verification Checklist

Run these checks on the host before you install the agent or before you approve a newly prepared host.

### Common Checks

```bash
node --version
npm --version
java -version
adb --version
adb devices
echo "${ANDROID_HOME:-}"
echo "${ANDROID_SDK_ROOT:-}"
```

What good looks like:

- `node` and `npm` both resolve successfully
- `java -version` reports Java 11 or newer
- `adb` is available
- connected Android devices appear in `adb devices` without `unauthorized`
- at least one of `ANDROID_HOME` or `ANDROID_SDK_ROOT` points at the SDK root, or the SDK is installed in a standard path the agent can auto-detect

### Optional Android Emulator Check

```bash
emulator -list-avds
```

Use this when the host is expected to run Android emulators. It confirms the emulator binary is installed and the AVD list is visible.

### macOS-Only Checks

```bash
xcodebuild -version
xcrun simctl list devices available | head
ios --version
```

What good looks like:

- `xcodebuild` resolves and reports the active Xcode version
- `xcrun simctl` can list available simulator devices
- `ios --version` reports the installed `go-ios` CLI version for Apple real-device telemetry

## Related Docs

- [Host Onboarding](host-onboarding.md)
- [Hosts And Host Detail Operations](hosts-and-host-detail-operations.md)
- [Deployment Guide](../deployment.md)
- [Environment Reference](../reference/environment.md)
- [Settings Reference](../reference/settings.md)
