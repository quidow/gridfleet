# Testkit Reference

`testkit/` is the supported Python integration surface for external pytest/Appium suites in this repository.

## Support Boundary

- Supported public package: `gridfleet-testkit`
- Supported import root: `gridfleet_testkit`
- Supported pytest plugin: `gridfleet_testkit.pytest_plugin`
- Supported pytest fixtures: `appium_driver`, `gridfleet_client`, `gridfleet_client_config`, `device_test_data`, `device_handle`, `gridfleet_worker_id`
- Supported public Appium helpers: `build_appium_options`, `create_appium_driver`, `get_device_test_data_for_driver`
- Supported public client helpers: `GridFleetClient`, `HeartbeatThread`, `register_run_cleanup`
- Supported public device/session helpers: `Device` (return type of `get_device` / `list_devices`), `resolve_device_handle_from_driver`
- Supported public result types: `CooldownResult`, `CooldownSetResult`, `CooldownEscalatedResult`
- Supported environment variables: `GRID_URL`, `GRIDFLEET_API_URL`, `GRIDFLEET_TESTKIT_USERNAME`, `GRIDFLEET_TESTKIT_PASSWORD`, `GRIDFLEET_TESTKIT_PACK_ID`, `GRIDFLEET_TESTKIT_PLATFORM_ID`, `GRIDFLEET_RUN_ID`
- Manual hardware examples live under `testkit/examples/`

The example screenshot scripts are examples, not CI-backed conformance tests. The maintained support promise is the installable package and documented import pattern.

## Install

From PyPI:

```bash
pip install gridfleet-testkit
```

From a local checkout:

```bash
uv pip install -e ./testkit
```

From a Git checkout or VCS URL that includes this package:

```bash
uv pip install "git+https://github.com/<org>/<repo>.git#subdirectory=testkit"
```

`Appium-Python-Client` is a runtime dependency because the pytest fixture creates real Appium sessions.
The package supports Python 3.10 through 3.14.

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
- accepts an overridable `gridfleet_client_config` fixture (default `None`) to tune the HTTP transport (connection retries, timeouts, proxy, TLS) for every session; the testkit still owns the endpoint
- exposes `device_test_data` for post-session operator-attached test data using the runtime connection target
- exposes `gridfleet_worker_id` which returns the pytest-xdist worker id, or `"controller"` for non-worker processes
- relies on manager-owned session target isolation for driver-sensitive ports and XCUITest build paths on managed nodes

If Appium driver creation fails before a Grid session exists, the exception propagates directly to the test. The router/grid allocation flow owns session-row lifecycle; pre-session failures are not recorded by the testkit.

## Direct Appium Usage

For scripts or non-pytest tools, the package also exposes public Appium helpers:

```python
from gridfleet_testkit import create_appium_driver, get_device_test_data_for_driver

driver = create_appium_driver(
    pack_id="appium-uiautomator2",
    platform_id="android_mobile",
    test_name="manual-smoke",
)

test_data = get_device_test_data_for_driver(driver)
```

Those helpers reuse the same driver-pack catalog resolver as the pytest fixture. Managed nodes still receive their host-scoped parallel-safe session defaults from the manager at session startup; callers should not hard-code `systemPort`, `chromedriverPort`, `mjpegServerPort`, `wdaLocalPort`, or `derivedDataPath`. `get_device_test_data_for_driver(...)` is the direct-driver equivalent of the pytest `device_test_data` fixture.

## Environment Variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `GRID_URL` | `http://localhost:4444` | WebDriver router URL used by the Appium fixture |
| `GRIDFLEET_API_URL` | `http://localhost:8000/api` | GridFleet API base used for session reporting, run helpers, and driver-pack catalog lookup |
| `GRIDFLEET_TESTKIT_USERNAME` | unset | Machine-auth username sent as HTTP Basic auth on every API call. Required when the manager runs with `GRIDFLEET_AUTH_ENABLED=true`. Use the same value as the manager's `GRIDFLEET_MACHINE_AUTH_USERNAME`. |
| `GRIDFLEET_TESTKIT_PASSWORD` | unset | Machine-auth password sent as HTTP Basic auth on every API call. Required when the manager runs with `GRIDFLEET_AUTH_ENABLED=true`. Use the same value as the manager's `GRIDFLEET_MACHINE_AUTH_PASSWORD`. |
| `GRIDFLEET_TESTKIT_PACK_ID` | unset | Optional default driver pack id for Appium option building |
| `GRIDFLEET_TESTKIT_PLATFORM_ID` | unset | Optional default platform id for Appium option building |
| `GRIDFLEET_RUN_ID` | unset | Optional run id. When set, drivers are created through the run-scoped grid endpoint `GRID_URL/run/{id}` so sessions land only on devices reserved for the run. Unset = free session on unreserved devices. Set externally by the run launcher/CI. |

The resolved URLs are also available programmatically via `gridfleet_testkit.grid_url()` and `gridfleet_testkit.api_url()`.

## Client Surface

| Helper | Purpose |
| --- | --- |
| `GridFleetClient.list_devices(*, pack_id=None, status=None, host_id=None, ...)` | List devices using backend keyword filters (pack_id, platform_id, status, host_id, connection_target, groups, ...); returns a list of typed `Device` objects. `groups` takes device-group keys and is sent as one repeated `group` parameter per key, ANDed by the backend |
| `GridFleetClient.get_device(device_id)` | Fetch one device as a typed `Device` (curated base fields) by backend device id |
| `GridFleetClient.get_device_test_data(device_id)` | Fetch operator-attached free-form test_data for a device |
| `GridFleetClient.replace_device_test_data(device_id, body)` | Replace test_data with the supplied object |
| `GridFleetClient.merge_device_test_data(device_id, body)` | Deep-merge into device test_data |
| `GridFleetClient.get_driver_pack_catalog()` | Fetch enabled driver-pack catalog data for Appium platform selection |
| `GridFleetClient.reserve_devices(...)` | Create a run/reservation and return the manager response |
| `GridFleetClient.get_run(run_id)` | Fetch one run detail row by backend run id |
| `GridFleetClient.signal_ready(run_id)` | Compatibility alias that moves a preparing run to `active` |
| `GridFleetClient.signal_active(run_id)` | Move a run from `preparing` to `active`, marking that real testing has begun. **Required** between preparation and real tests (a run left in `preparing` is eventually reaped as `never_activated`), but it is not a gate on device access — run-scoped sessions run on the run's reserved devices, and are linked to it, from `preparing` onward. |
| `GridFleetClient.heartbeat(run_id)` | Send a run heartbeat and read current state |
| `GridFleetClient.report_preparation_failure(run_id, device_id, message, source="ci_preparation")` | Exclude one reserved device after setup fails |
| `GridFleetClient.update_session_status(session_id, status)` | Report final session status |
| `GridFleetClient.complete_run(run_id)` | Complete a run |
| `GridFleetClient.cancel_run(run_id)` | Cancel a run |
| `GridFleetClient.cooldown_device(run_id, device_id, reason=..., ttl_seconds=...)` | Exclude a reserved device from the run with a cooldown TTL |
| `GridFleetClient.start_heartbeat(run_id, interval=30)` | Start a background heartbeat thread |
| `resolve_device_handle_from_driver(driver, client)` | Resolve the assigned device as a typed `Device` from a running Appium session |
| `register_run_cleanup(client, run_id, heartbeat_thread=None)` | Register `atexit` cleanup callable and return it; stops the heartbeat thread on exit but does not complete or cancel the run by default |

Public Appium helpers:

| Helper | Purpose |
| --- | --- |
| `build_appium_options(*, pack_id=None, platform_id=None, capabilities=None, test_name=None, catalog_client=None)` | Build an Appium options object for an explicit driver-pack platform |
| `create_appium_driver(*, pack_id=None, platform_id=None, capabilities=None, test_name=None, grid_url=None, catalog_client=None, client_config=None)` | Create an Appium remote driver through the WebDriver router for an explicit driver-pack platform. `client_config` (an `AppiumClientConfig`) tunes the HTTP transport (connection retries, timeouts, proxy); the testkit still owns the endpoint |
| `get_device_id_from_driver(driver)` | Resolve the backend device id from a live driver's `gridfleet:deviceId` session capability |
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

## Errors and Result Types

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

`signal_active` is the prep/test boundary as a lifecycle marker, not a device-access gate. Run-scoped sessions on reserved devices work from `preparing` onward — preparation work (build installs, smoke checks, warm-up Appium sessions) runs against the run's reserved devices and is linked to the run (it shows up in Run Detail). `signal_active` records that real testing has begun. If the client never calls `signal_active` and the run hits its TTL, the manager emits a `run.never_activated` event and the run expires with a clear error reason.

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

Router-routed runs no longer use per-worker claim/release calls. The pytest plugin composes the run-scoped grid endpoint (`GRID_URL/run/{run_id}`) from `GRIDFLEET_RUN_ID` and creates Appium sessions through it, so the router admits each session only to devices reserved for that run. Once a session is running, resolve the assigned manager device row from the driver's runtime connection target — the result is a typed `Device`.

```python
from gridfleet_testkit import GridFleetClient, resolve_device_handle_from_driver

client = GridFleetClient()
device = resolve_device_handle_from_driver(driver, client=client)
assert device.id
```

`Device` exposes the curated base fields (`id`, `identity_value`, `connection_target`, `name`, `pack_id`, `platform_id`, `platform_label`, `os_version`, `os_version_display`, `host_id`, `device_type`, `connection_type`, `manufacturer`, `model`, `operational_state`, `is_reserved`). `client.get_device(device_id)` and `client.list_devices(...)` return the same type. Group membership is not a device field — query it the other way round, by filtering `list_devices(groups=[...])` on the keys you care about.

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

The baseline examples intentionally share one simple flow — create session, print resolved connection context, save screenshot, and assert that the written file is non-empty — except the Roku baseline, which additionally installs and activates the bundled dev app before the screenshot.

## Platform-Specific Notes

- Android Mobile / Android TV / Fire TV:
  - require the UiAutomator2 driver on the host
  - use GridFleet routing metadata rather than hard-coded `appium:udid`
- Fire TV:
  - the baseline example supports optional `appium:os_version` filtering for Fire OS release selection
- iOS:
  - the baseline example uses the simulator lane with `appium:device_type=simulator`
- tvOS:
  - the baseline example selects the tvOS real device via `pack_id`/`platform_id` (`appium-xcuitest` / `tvos`) and does not set an explicit `appium:device_type` capability
  - XCUITest / WebDriverAgent prerequisites must already be configured on the macOS host
- Roku:
  - the baseline screenshot flow sideloads and activates the bundled dev app and requires Roku dev credentials in device config
  - the separate sideload example runs the same flow plus explicit print logging of the sideloaded app path
