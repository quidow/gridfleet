# Glossary

This glossary defines the product terms that show up across the API, UI, and operator guides.

| Term | Meaning |
| --- | --- |
| Host | A machine that runs the GridFleet agent and owns one or more registered devices |
| Device | A host-backed registry record for a test target, including platform, connectivity, readiness, and availability |
| Identity kind | The kind of stable identity the manager persists for a device, such as `adb_serial`, `apple_udid`, or `roku_serial` |
| Identity value | The stable persisted identifier for a device in the registry |
| Connection target | The current transport target the manager and Appium use to reach the device; this can differ from the persisted identity |
| Readiness state | The high-level testability state computed from setup requirements and verification status: `setup_required`, `verification_required`, or `verified` |
| Verification job | The staged async workflow that validates a new or edited device before it is considered ready for use |
| Appium node | The managed Appium process registered to Selenium Grid for a specific device |
| Session | A recorded Appium/Grid session linked to one device and its final test outcome |
| Run | A reservation record that locks one or more matching devices for an external test workflow |
| Reservation | The period where a run owns devices and prevents other runs from matching them |
| Lifecycle incident | A recent persisted device-event record used to explain deferred stops, backoff, exclusions, and recoveries |
| Maintenance | An operator-controlled state that blocks normal use while the device is intentionally withheld from testing |
| Auto-manage | The flag that allows the manager to start/stop/recover a device’s Appium node automatically |
| Intake candidate | A host-visible device detected by the agent but not yet imported into the canonical registry |
| Discovery | The host-scoped scan that compares what the agent sees to what the manager already has persisted |
| Config template | A reusable named config payload that can be applied to one or more devices |
| Device group | A saved static or dynamic set of devices used for operator organization and bulk actions |
| Static group | A group whose members are explicitly added and removed |
| Dynamic group | A group whose members are resolved from filter rules at read time |
| Lifecycle policy summary | The operator-facing summary of whether a device is idle, backoff-limited, excluded, suppressed, recoverable, or otherwise impacted by lifecycle automation |

## Identity And Transport

- The registry persists `identity_value` and optional `connection_target`.
- Appium-facing `appium:udid` is derived from the saved connection target or the running node's active target.
- For network Android/Fire TV devices, the connection target is often an `ip:port` ADB target.
- For Android emulators and Apple simulators, the registry persists `connection_type=virtual` instead of overloading USB or network semantics.
- For Android emulators, the registry persists a stable `avd:<name>` identity and a host-local AVD-name connection target; uniqueness is enforced per host, while running Appium nodes track the current ADB serial separately.

## Readiness And Availability

- Readiness answers whether the saved device configuration is safe to use.
- Availability answers whether the device is currently available, busy, offline, reserved, or in maintenance.
- A device can exist in the registry before it is verified, but most operational flows require `verified` readiness.
