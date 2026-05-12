# Testkit Reference

`testkit/` is the supported Python integration surface for external pytest/Appium suites in this repository.

## Support Boundary

- Supported public package: `gridfleet-testkit`
- Supported import root: `gridfleet_testkit`
- Supported pytest plugin: `gridfleet_testkit.pytest_plugin`
- Supported pytest fixtures: `appium_driver`, `gridfleet_client`, `device_config`, `device_test_data`, `device_handle`, `gridfleet_worker_id`
- Supported public Appium helpers: `build_appium_options`, `create_appium_driver`, `get_connection_target_from_driver`, `get_device_config_for_driver`, `get_device_test_data_for_driver`
- Supported public client helpers: `GridFleetClient`, `HeartbeatThread`, `register_run_cleanup`
- Supported public allocation/session helpers: `AllocatedDevice`, `UnavailableInclude`, `build_error_session_payload`, `hydrate_allocated_device`, `hydrate_allocated_device_from_driver`, `resolve_device_handle_from_driver`
- Supported public result types: `CooldownResult`, `CooldownSetResult`, `CooldownEscalatedResult`
- Supported public exceptions: `UnknownIncludeError`, `ReserveCapabilitiesUnsupportedError`
- Supported environment variables: `GRID_URL`, `GRIDFLEET_API_URL`, `GRIDFLEET_TESTKIT_USERNAME`, `GRIDFLEET_TESTKIT_PASSWORD`, `GRIDFLEET_TESTKIT_PACK_ID`, `GRIDFLEET_TESTKIT_PLATFORM_ID`
- Manual hardware examples live under `testkit/examples/`

The example screenshot scripts are examples, not CI-backed conformance tests. The maintained support promise is the installable package and documented import pattern.

## Install

From PyPI:

```bash
pip install "gridfleet-testkit[appium]"
```

From a local checkout:

```bash
uv pip install -e ./testkit[appium]
```

From a Git checkout or VCS URL that includes this package:

```bash
uv pip install "git+https://github.com/<org>/<repo>.git#subdirectory=testkit"
```

`Appium-Python-Client` is included via the `appium` extra because the pytest fixture creates real Appium sessions.
The package supports Python 3.10 and newer.

## Public Imports

```python
from gridfleet_testkit import GridFleetClient, create_appium_driver, register_run_cleanup
```

Use the supported pytest plugin path instead of importing fixtures from an internal file path:

```python
pytest_plugins = ["gridfleet_testkit.pytest_plugin"]
```

## Pytest Integration

Minimal `conftest.py`:

```python
pytest_plugins = ["gridfleet_testkit.pytest_plugin"]
```

Minimal test:

```python
import pytest

@pytest.mark.parametrize(
    "appium_driver",
    [{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"}],
    indirect=True,
)
def test_session_starts(appium_driver):
    assert appium_driver.session_id is not None
```

The plugin supports three ways to describe the target platform:

- Preferred explicit selector: `pack_id` + `platform_id`
  - resolves against enabled driver-pack catalog data
  - injects Appium `platformName`, `appium:automationName`, and `appium:platform`
- Unambiguous shorthand: `platform_id`
  - accepted only when exactly one enabled pack provides that platform id
- Environment defaults: set `GRIDFLEET_TESTKIT_PACK_ID` and `GRIDFLEET_TESTKIT_PLATFORM_ID`, then parametrize with `{}`
- Escape hatch: `platformName`
  - pass this as a normal capability key when you want raw Appium control instead of catalog selection

The plugin:

- creates an Appium session through `GRID_URL`
- injects `gridfleet:testName` with the pytest test name
- reports final session status back to `GRIDFLEET_API_URL`
- exposes `device_config` for post-session device-config lookup using live `appium:udid`
- exposes `device_test_data` for post-session operator-attached test data using the runtime connection target
- exposes `gridfleet_worker_id` which returns the pytest-xdist worker id, or `"controller"` for non-worker processes
- relies on manager-owned session target isolation for driver-sensitive ports and XCUITest build paths on managed nodes

If Appium driver creation fails before a Grid session exists, the pytest fixture registers a device-less terminal error session with an `error-<uuid>` session id, attempted capabilities, requested pack/platform metadata when available, and exception details, then re-raises the original exception. These rows make setup failures visible in the GridFleet Sessions view.

## Direct Appium Usage

For scripts or non-pytest tools, the package also exposes public Appium helpers:

```python
from gridfleet_testkit import create_appium_driver, get_device_config_for_driver

driver = create_appium_driver(
    pack_id="appium-uiautomator2",
    platform_id="android_mobile",
    test_name="manual-smoke",
)

device_config = get_device_config_for_driver(driver)
```

Those helpers reuse the same driver-pack catalog resolver as the pytest fixture. Managed nodes still receive their host-scoped parallel-safe session defaults from the manager at session startup; callers should not hard-code `systemPort`, `chromedriverPort`, `mjpegServerPort`, `wdaLocalPort`, or `derivedDataPath`. `get_device_config_for_driver(...)` is the direct-driver equivalent of the pytest `device_config` fixture.

## Environment Variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `GRID_URL` | `http://localhost:4444` | Selenium Grid hub URL used by the Appium fixture |
| `GRIDFLEET_API_URL` | `http://localhost:8000/api` | GridFleet API base used for session reporting, config lookup, run helpers, and driver-pack catalog lookup |
| `GRIDFLEET_TESTKIT_USERNAME` | unset | Machine-auth username sent as HTTP Basic auth on every API call. Required when the manager runs with `GRIDFLEET_AUTH_ENABLED=true`. Use the same value as the manager's `GRIDFLEET_MACHINE_AUTH_USERNAME`. |
| `GRIDFLEET_TESTKIT_PASSWORD` | unset | Machine-auth password sent as HTTP Basic auth on every API call. Required when the manager runs with `GRIDFLEET_AUTH_ENABLED=true`. Use the same value as the manager's `GRIDFLEET_MACHINE_AUTH_PASSWORD`. |
| `GRIDFLEET_TESTKIT_PACK_ID` | unset | Optional default driver pack id for Appium option building |
| `GRIDFLEET_TESTKIT_PLATFORM_ID` | unset | Optional default platform id for Appium option building |

## Client Surface

| Helper | Purpose |
| --- | --- |
| `GridFleetClient.list_devices(*, pack_id=None, status=None, host_id=None, ...)` | List devices using backend keyword filters (pack_id, platform_id, status, host_id, connection_target, tags, ...) |
| `GridFleetClient.get_device(device_id)` | Fetch one full device detail row by backend device id |
| `GridFleetClient.get_device_config(connection_target)` | Look up a device by active connection target, then fetch its config |
| `GridFleetClient.get_device_capabilities(device_id)` | Fetch current Appium capability metadata for a device |
| `GridFleetClient.get_device_test_data(device_id)` | Fetch operator-attached free-form test_data for a device |
| `GridFleetClient.replace_device_test_data(device_id, body)` | Replace test_data with the supplied object |
| `GridFleetClient.merge_device_test_data(device_id, body)` | Deep-merge into device test_data |
| `GridFleetClient.resolve_device_id_by_connection_target(connection_target)` | Resolve the backend device id for a runtime connection target |
| `GridFleetClient.get_device_by_connection_target(connection_target)` | Fetch one device detail row by runtime connection target |
| `GridFleetClient.get_driver_pack_catalog()` | Fetch enabled driver-pack catalog data for Appium platform selection |
| `GridFleetClient.reserve_devices(...)` | Create a run/reservation and return the manager response |
| `GridFleetClient.signal_ready(run_id)` | Compatibility alias that moves a preparing run to `active` |
| `GridFleetClient.signal_active(run_id)` | Move a run to `active` |
| `GridFleetClient.heartbeat(run_id)` | Send a run heartbeat and read current state |
| `GridFleetClient.report_preparation_failure(run_id, device_id, message, source="ci_preparation")` | Exclude one reserved device after setup fails |
| `GridFleetClient.register_session(fields)` | Register a Grid/Appium session with optional requested capability metadata |
| `GridFleetClient.register_session_from_driver(driver, fields)` | Extract session id and capabilities from an Appium driver and register the session |
| `GridFleetClient.update_session_status(session_id, status)` | Report final session status |
| `GridFleetClient.complete_run(run_id)` | Complete a run |
| `GridFleetClient.cancel_run(run_id)` | Cancel a run |
| `GridFleetClient.cooldown_device(run_id, device_id, reason=..., ttl_seconds=...)` | Exclude a reserved device from the run with a cooldown TTL |
| `GridFleetClient.start_heartbeat(run_id, interval=30)` | Start a background heartbeat thread |
| `build_error_session_payload(fields)` | Build a `/api/sessions` payload for driver-creation failures without importing pytest |
| `hydrate_allocated_device(device_handle, run_id, client)` | Combine a device handle with optional device config and live capabilities |
| `hydrate_allocated_device_from_driver(allocated, driver, client)` | Return a new allocated-device object with capabilities from a running driver |
| `resolve_device_handle_from_driver(driver, client)` | Resolve the assigned manager device row from a running Appium session |
| `register_run_cleanup(client, run_id, heartbeat_thread=None)` | Register `atexit` cleanup callable and return it; stops the heartbeat thread on exit but does not complete or cancel the run by default |

Public Appium helpers:

| Helper | Purpose |
| --- | --- |
| `build_appium_options(*, pack_id=None, platform_id=None, capabilities=None, test_name=None, catalog_client=None)` | Build an Appium options object for an explicit driver-pack platform |
| `create_appium_driver(*, pack_id=None, platform_id=None, capabilities=None, test_name=None, grid_url=None, catalog_client=None)` | Create an Appium remote driver through Selenium Grid for an explicit driver-pack platform |
| `get_connection_target_from_driver(driver)` | Read the active connection target from a live Appium session |
| `get_device_config_for_driver(driver, gridfleet_client=None)` | Fetch device config for a live Appium session using its active connection target |
| `get_device_test_data_for_driver(driver, gridfleet_client=None)` | Fetch test_data for a live Appium driver |

## Run Cleanup Policy

`register_run_cleanup(...)` registers an atexit cleanup callable and returns it. By default it stops the heartbeat thread but does not complete or cancel the run, because process exit alone does not prove test success. Prefer explicit `client.complete_run(run_id)` after successful orchestration and `client.cancel_run(run_id)` for known failures. Pass `on_exit="complete"` or `on_exit="cancel"` only when that policy is correct for your script. Signal handlers are opt-in with `install_signal_handlers=True`; signal cleanup defaults to cancellation.

## Device Test Data

The `device_test_data` fixture returns the operator-attached free-form test_data for the device assigned to the current test:

```python
def test_uses_operator_data(appium_driver, device_test_data):
    assert "account" in device_test_data
```

Outside of pytest, use the client directly:

```python
test_data = client.get_device_test_data(device_id)
```

Or use the driver helper after a session is up:

```python
from gridfleet_testkit import get_device_test_data_for_driver

test_data = get_device_test_data_for_driver(driver)
```

Pass `fetch_test_data=True` to `hydrate_allocated_device(...)` to populate `allocated.test_data` inline when it is not already present on the supplied device handle.

## Errors and Result Types

- `UnknownIncludeError(ValueError)`: raised when the backend rejects one or more `?include=` keys. Exposes `values` with the rejected key names. The `ValueError` base is part of the contract.
- `ReserveCapabilitiesUnsupportedError(ValueError)`: raised when a reserve-time `include` request contains `"capabilities"`, which is not supported at reserve time. The `ValueError` base is part of the contract.
- `CooldownResult`: union response type from `cooldown_device`, with `status` equal to `"cooldown_set"` or `"maintenance_escalated"`. `CooldownSetResult` and `CooldownEscalatedResult` are the concrete TypedDict variants.

## Example Reservation Flow

```python
from gridfleet_testkit import GridFleetClient, register_run_cleanup

client = GridFleetClient("http://manager-ip:8000/api")

run = client.reserve_devices(
    name="my-test-run",
    requirements=[
        {
            "pack_id": "appium-uiautomator2",
            "platform_id": "firetv_real",
            "os_version": "8",
            "allocation": "all_available",
            "min_count": 1,
        }
    ],
    ttl_minutes=45,
    created_by="local-dev",
)

run_id = run["id"]
worker_count = len(run["devices"])
heartbeat_thread = client.start_heartbeat(run_id, interval=30)
cleanup = register_run_cleanup(client, run_id, heartbeat_thread)
# cleanup() runs at process exit; call client.complete_run(run_id) on success
# or client.cancel_run(run_id) on failure to set the run state explicitly.

client.report_preparation_failure(
    run_id,
    device_id="device-123",
    message="Driver bootstrap timed out during CI setup",
    source="local-dev",
)

client.signal_active(run_id)
```

Use `count` for exact reservations. Use `allocation: "all_available"` when CI should reserve every currently eligible matching device and size its worker pool from `len(run["devices"])`.

## Cooling Down Unstable Devices

If a reserved device becomes unstable during a test, call `cooldown_device` to exclude it from the run for a TTL. Repeated cooldowns on the same device within the same run may escalate the device to maintenance when the threshold is reached.

```python
from gridfleet_testkit import GridFleetClient

client = GridFleetClient()
result = client.cooldown_device(
    run_id="run-123",
    device_id="device-456",
    reason="Connection dropped mid-test",
    ttl_seconds=120,
)
```

The response is a `CooldownResult` union:

```python
# Normal cooldown
{"status": "cooldown_set", "excluded_until": "2026-05-12T10:00:00Z", "cooldown_count": 2}

# Escalated to maintenance
{"status": "maintenance_escalated", "cooldown_count": 3, "threshold": 3}
```

The manager enforces a maximum TTL via `general.device_cooldown_max_sec` (default 3600s).

## Device Handles

Grid-routed runs no longer use per-worker claim/release calls. The pytest plugin injects `gridfleet:run_id` into Appium capabilities, so Selenium Grid routes new sessions to nodes reserved for that run. Once a session is running, resolve the assigned manager device row from the driver's runtime connection target.

```python
from gridfleet_testkit import GridFleetClient, hydrate_allocated_device, resolve_device_handle_from_driver

client = GridFleetClient()
device_handle = resolve_device_handle_from_driver(driver, client=client)
allocated = hydrate_allocated_device(device_handle, run_id=run_id, client=client)
```

`device_config` and inline `config` payloads are returned verbatim from the manager. The testkit does not perform client-side secret masking or reveal toggles. Protect device config with manager authentication, operator access control, and your lab's secret-handling policy.

`reserve_devices` accepts `include=("config",)` only — `include=("capabilities",)` raises `ReserveCapabilitiesUnsupportedError` client-side because reserve-time capabilities are not yet device-bound.

`include=` must be a sequence of strings (tuple or list) — order is preserved in the emitted query parameter. Passing a bare string like `include="config"` raises `TypeError` to avoid silently splitting the value into characters.

`hydrate_allocated_device` accepts device-handle payloads such as `reserve_response["devices"]` entries or rows returned by `get_device_by_connection_target`.

## Examples

Baseline screenshot examples:

- `testkit/examples/test_android_mobile_screenshot.py`
- `testkit/examples/test_android_tv_screenshot.py`
- `testkit/examples/test_firetv_screenshot.py`
- `testkit/examples/test_ios_simulator_screenshot.py`
- `testkit/examples/test_tvos_screenshot.py`
- `testkit/examples/test_roku_screenshot.py`

Advanced example:

- `testkit/examples/test_roku_sideload_screenshot.py`

The baseline examples intentionally share one simple flow: create session, print resolved connection context, save screenshot, and assert that the written file is non-empty.

## Platform-Specific Notes

- Android Mobile / Android TV / Fire TV:
  - require the UiAutomator2 driver on the host
  - use GridFleet routing metadata rather than hard-coded `appium:udid`
- Fire TV:
  - the baseline example supports optional `appium:os_version` filtering for Fire OS release selection
- iOS:
  - the baseline example uses the simulator lane with `appium:device_type=simulator`
- tvOS:
  - the baseline example uses the real-device lane with `appium:device_type=real_device`
  - XCUITest / WebDriverAgent prerequisites must already be configured on the macOS host
- Roku:
  - baseline screenshot flow does not imply sideload is required
  - the separate sideload example still expects Roku dev credentials in device config
