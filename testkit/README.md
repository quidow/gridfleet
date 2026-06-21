# GridFleet Testkit

`testkit/` is the supported Python integration surface for external pytest/Appium suites that run through GridFleet.

## What This Package Owns

- Stable import root: `gridfleet_testkit`
- Supported pytest plugin: `gridfleet_testkit.pytest_plugin`
- Supported pytest fixtures: `appium_driver`, `gridfleet_client`, `gridfleet_client_config`, `device_test_data`, `device_handle`, `gridfleet_worker_id`
- Supported public Appium helpers:
  - `build_appium_options`
  - `create_appium_driver`
  - `get_device_test_data_for_driver`
- Supported public client helpers:
  - `GridFleetClient`
  - `HeartbeatThread`
  - `register_run_cleanup`
- Supported public device/session helpers:
  - `Device` (return type of `get_device` / `list_devices`)
  - `resolve_device_handle_from_driver`
- Supported public result types:
  - `CooldownResult`
  - `CooldownSetResult`
  - `CooldownEscalatedResult`
- Manual hardware examples under `testkit/examples/`

## What It Does Not Own

- Appium server installation or host-level driver setup
- WebDriver router lifecycle
- Device registration, verification, or readiness setup
- CI orchestration beyond the documented client helpers

The supported contract is the installable package and documented import pattern. The example scripts are onboarding aids, not CI-backed conformance tests.

## Install

From PyPI:

```bash
pip install "gridfleet-testkit"
```

From a local checkout:

```bash
uv pip install -e ./testkit
```

From a copied `testkit/` directory inside another repository:

```bash
uv pip install -e ./testkit
```

From a Git checkout or VCS URL that contains this package:

```bash
uv pip install "git+https://github.com/<org>/<repo>.git#subdirectory=testkit"
```

The package supports Python 3.10 through 3.14.
`Appium-Python-Client` is installed as a runtime dependency because the pytest fixtures create real Appium sessions.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `GRID_URL` | `http://localhost:4444` | WebDriver router URL used by the pytest Appium fixture |
| `GRIDFLEET_API_URL` | `http://localhost:8000/api` | GridFleet API base used for session reporting, run helpers, and driver-pack catalog lookup |
| `GRIDFLEET_TESTKIT_USERNAME` | unset | Machine-auth username sent as HTTP Basic auth on every API call. Required when the manager runs with `GRIDFLEET_AUTH_ENABLED=true`. Use the same value as the manager's `GRIDFLEET_MACHINE_AUTH_USERNAME`. |
| `GRIDFLEET_TESTKIT_PASSWORD` | unset | Machine-auth password sent as HTTP Basic auth on every API call. Required when the manager runs with `GRIDFLEET_AUTH_ENABLED=true`. Use the same value as the manager's `GRIDFLEET_MACHINE_AUTH_PASSWORD`. |
| `GRIDFLEET_TESTKIT_PACK_ID` | unset | Optional default driver pack id for Appium option building |
| `GRIDFLEET_TESTKIT_PLATFORM_ID` | unset | Optional default platform id for Appium option building |
| `GRIDFLEET_RUN_ID` | unset | Optional run id. When set, drivers are created through the run-scoped grid endpoint `GRID_URL/run/{id}` so sessions land only on devices reserved for the run. Unset = free session on unreserved devices. Set this in the environment that launches pytest (e.g. the run launcher or CI step); the testkit reads it but does not set it. |

The package assumes a running GridFleet API, a reachable WebDriver router, and platform-specific Appium driver setup on the registered hosts. When auth is disabled on the manager, leave `GRIDFLEET_TESTKIT_USERNAME` / `GRIDFLEET_TESTKIT_PASSWORD` unset and the testkit will send no `Authorization` header.

## Pytest Plugin

Load the supported plugin from your test project:

```python
pytest_plugins = ["gridfleet_testkit.pytest_plugin"]
```

Minimal usage:

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

The plugin resolves `pack_id` and `platform_id` against the enabled driver-pack catalog, then injects Appium `platformName`, `appium:automationName`, `appium:platform`, and `gridfleet:testName`.

When exactly one enabled pack provides a platform id, `platform_id` alone is accepted. For environment-portable tests, set `GRIDFLEET_TESTKIT_PACK_ID` and `GRIDFLEET_TESTKIT_PLATFORM_ID`, then parametrize with `{}`.

If you need raw Appium control instead, omit `pack_id` and `platform_id`, then pass `platformName` as a normal capability key.

### Tuning the HTTP Transport

To tune the Appium HTTP transport for every `appium_driver` session — connection retries, timeouts, proxy, TLS — override the `gridfleet_client_config` fixture in your `conftest.py`. It defaults to `None`. The testkit still owns the endpoint, so any `remote_server_addr` you set is overwritten with the resolved grid URL:

```python
# conftest.py
import pytest
from appium.webdriver.client_config import AppiumClientConfig
from urllib3.util.retry import Retry

@pytest.fixture
def gridfleet_client_config():
    return AppiumClientConfig(
        remote_server_addr="",  # overwritten by the testkit
        init_args_for_pool_manager={"init_args_for_pool_manager": {"retries": Retry(total=3, backoff_factor=0.5)}},
    )
```

### Plugin Lifecycle

- Creates an Appium session through `GRID_URL`
- Injects `gridfleet:testName` with the pytest test name
- Resolves the WebDriver endpoint from `GRIDFLEET_RUN_ID`: run-scoped URL inside a reserved run, bare grid URL otherwise. No GridFleet identity is injected into capabilities.
- Reports final session status back to `GRIDFLEET_API_URL`
- Exposes `device_test_data` for post-session operator-attached test data using the runtime connection target
- Exposes `device_handle` (a typed `Device`) for the device the live session landed on
- Exposes `gridfleet_worker_id` which returns the pytest-xdist worker id, or `"controller"` for non-worker processes
- Relies on manager-owned runtime isolation for Appium driver sub-ports and XCUITest build paths

If Appium driver creation fails before a Grid session exists, the exception propagates directly to the test. The router/grid allocation flow owns session-row lifecycle; pre-session failures are not recorded by the testkit.

## Direct Appium Usage

If you need to create a driver outside pytest, use the public Appium helpers:

```python
from gridfleet_testkit import create_appium_driver, get_device_test_data_for_driver

driver = create_appium_driver(
    pack_id="appium-uiautomator2",
    platform_id="firetv_real",
    test_name="manual-smoke",
)

try:
    assert driver.session_id is not None
    test_data = get_device_test_data_for_driver(driver)
finally:
    driver.quit()
```

`create_appium_driver(...)` reuses the same driver-pack catalog resolver as the pytest fixture. Managed nodes still get their host-scoped runtime allocations from the manager, so callers should not hard-code `systemPort`, `chromedriverPort`, `mjpegServerPort`, `wdaLocalPort`, or `derivedDataPath`. `get_device_test_data_for_driver(...)` is the non-pytest equivalent of the `device_test_data` fixture. If you only need the options object, use `build_appium_options(...)`.

To tune the HTTP transport — connection retries, timeouts, proxy, TLS — pass an `AppiumClientConfig` via `client_config`. The testkit still owns the endpoint, so any `remote_server_addr` you set is overwritten with the resolved grid URL:

```python
from appium.webdriver.client_config import AppiumClientConfig
from urllib3.util.retry import Retry

client_config = AppiumClientConfig(
    remote_server_addr="",  # overwritten by the testkit
    init_args_for_pool_manager={"init_args_for_pool_manager": {"retries": Retry(total=3, backoff_factor=0.5)}},
)

driver = create_appium_driver(
    pack_id="appium-uiautomator2",
    platform_id="firetv_real",
    client_config=client_config,
)
```

## Client Helpers

| Helper | Purpose |
| --- | --- |
| `GridFleetClient.list_devices(*, pack_id=None, status=None, host_id=None, ...)` | List devices using backend keyword filters (pack_id, platform_id, status, host_id, connection_target, tags, ...); returns a list of typed `Device` objects |
| `GridFleetClient.get_device(device_id)` | Fetch one device as a typed `Device` (curated base fields) by backend device id |
| `GridFleetClient.get_device_test_data(device_id)` | Fetch operator-attached free-form test_data for a device |
| `GridFleetClient.get_run(run_id)` | Fetch one run detail row by backend run id |
| `GridFleetClient.replace_device_test_data(device_id, body)` | Replace test_data with the supplied object |
| `GridFleetClient.merge_device_test_data(device_id, body)` | Deep-merge into device test_data |
| `GridFleetClient.get_driver_pack_catalog()` | Fetch enabled driver-pack catalog data for Appium platform selection |
| `GridFleetClient.reserve_devices(...)` | Create a run/reservation and return the manager response |
| `GridFleetClient.signal_ready(run_id)` | Signal that a run is ready |
| `GridFleetClient.signal_active(run_id)` | Move a run to `active` |
| `GridFleetClient.heartbeat(run_id)` | Send a run heartbeat and read current state |
| `GridFleetClient.report_preparation_failure(run_id, device_id, message, source="ci_preparation")` | Exclude one reserved device after setup fails |
| `GridFleetClient.update_session_status(session_id, status)` | Report final session status |
| `GridFleetClient.complete_run(run_id)` | Complete a run |
| `GridFleetClient.cancel_run(run_id)` | Cancel a run |
| `GridFleetClient.cooldown_device(run_id, device_id, reason=..., ttl_seconds=...)` | Exclude a reserved device from the run with a cooldown TTL |
| `GridFleetClient.start_heartbeat(run_id, interval=30)` | Start a background heartbeat thread |
| `get_device_id_from_driver(driver)` | Resolve the backend device id from a live driver's `gridfleet:deviceId` session capability |
| `resolve_device_handle_from_driver(driver, client)` | Resolve the assigned device as a typed `Device` from a running Appium session |
| `get_device_test_data_for_driver(driver, gridfleet_client=None)` | Fetch test_data for a live Appium driver |
| `register_run_cleanup(client, run_id, heartbeat_thread=None)` | Register `atexit` cleanup callable and return it; stops the heartbeat thread on exit but does not complete or cancel the run by default |

### Targeting Devices by Tag

GridFleet injects device tags into node stereotypes as `gridfleet:tag:<key>` capabilities, so the router's backend allocation can route sessions to devices matching specific tags.

```python
@pytest.mark.parametrize(
    "appium_driver",
    [
        {
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
            "gridfleet:tag:screen_type": "4k",
        }
    ],
    indirect=True,
)
def test_4k_display(appium_driver):
    ...
```

The same capability works for free sessions:

```python
driver = create_appium_driver(
    pack_id="appium-uiautomator2",
    platform_id="android_mobile",
    capabilities={"gridfleet:tag:screen_type": "4k"},
)
```

When an operator edits device tags, GridFleet marks the device for re-verification. The next verification restarts the Appium node and re-registers it with the updated Grid stereotype.

### Worker Identity

The `gridfleet_worker_id` fixture is informational only: it returns the pytest-xdist worker id (normally `gw0`, `gw1`, and so on), or `"controller"` for non-worker processes. It is never transmitted to the manager; use it client-side for local sharding or log correlation. For run attribution, pass the `created_by` argument to `GridFleetClient.reserve_devices` — that is the only run-attribution field the reservation request carries.

### Reservation Flow

GridFleet runs are router-routed: once devices are reserved, the manager tags matching nodes with the run id, and the router routes new Appium sessions to those nodes automatically when they arrive through the run-scoped endpoint (`GRID_URL/run/{run_id}`). There are no per-worker claim or release calls.

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

# If one reserved device fails setup:
client.report_preparation_failure(
    run_id,
    device_id="device-123",
    message="Driver bootstrap timed out during CI setup",
    source="local-dev",
)

client.signal_ready(run_id)
client.signal_active(run_id)
```

Use `count` for exact reservations. Use `allocation: "all_available"` when CI should reserve every currently eligible matching device and size its worker pool from `len(run["devices"])`.

For pytest-xdist controller/worker orchestration, see [Testkit xdist recipe](../docs/guides/testkit-xdist-recipe.md). The recipe is copyable guidance, not a public testkit abstraction.

### Cooling Down an Unstable Device

If a reserved device becomes unstable during a test, you can put it on cooldown so it is excluded from the run for a TTL. If the same device is cooled down too many times in the same run, it is escalated to maintenance automatically.

```python
from gridfleet_testkit import GridFleetClient

client = GridFleetClient()

result = client.cooldown_device(
    run_id="run-123",
    device_id="device-456",
    reason="Connection dropped mid-test",
    ttl_seconds=120,
)

if result["status"] == "cooldown_set":
    print(f"Device on cooldown until {result['excluded_until']}")
elif result["status"] == "maintenance_escalated":
    print(f"Escalated after {result['cooldown_count']} cooldowns")
```

The manager enforces a maximum TTL via the `general.device_cooldown_max_sec` setting. The default is 3600 seconds. An `httpx.HTTPStatusError` with status 422 is raised if `ttl_seconds` exceeds the maximum.

### Typed Device Reads

`get_device` and `list_devices` return typed `Device` objects so callers see the available fields in their IDE instead of guessing dict keys.

```python
from gridfleet_testkit import GridFleetClient, resolve_device_handle_from_driver

client = GridFleetClient()

# After creating an Appium session, resolve the assigned device row
device = resolve_device_handle_from_driver(driver, client=client)
assert device.id
assert device.platform_label in {"Android", "iOS", "tvOS", "Roku", None}

# Or fetch / list directly
one = client.get_device(device.id)
available = client.list_devices(status="available")
```

`Device` carries the curated base field set both endpoints emit (`id`, `identity_value`, `connection_target`, `name`, `pack_id`, `platform_id`, `platform_label`, `os_version`, `os_version_display`, `host_id`, `device_type`, `connection_type`, `manufacturer`, `model`, `tags`, `operational_state`, `is_reserved`). Volatile long-tail fields (battery, telemetry, readiness, health summary, ...) are intentionally not surfaced. For operator-attached free-form data, use `client.get_device_test_data(device.id)` or the `device_test_data` fixture.

### Run Cleanup Policy

`register_run_cleanup(...)` registers an atexit cleanup callable and returns it. By default it stops the heartbeat thread but does not complete or cancel the run, because process exit alone does not prove test success. Prefer explicit `client.complete_run(run_id)` after successful orchestration and `client.cancel_run(run_id)` for known failures. Pass `on_exit="complete"` or `on_exit="cancel"` only when that policy is correct for your script. Signal handlers are opt-in with `install_signal_handlers=True`; signal cleanup defaults to cancellation.

### Device Test Data

The `device_test_data` fixture returns the operator-attached free-form test_data for the device assigned to the current test:

```python
def test_uses_operator_data(appium_driver, device_test_data):
    assert "account" in device_test_data
```

Outside of pytest, use the client directly:

```python
test_data = client.get_device_test_data(device_id)
```

Or use the driver helper:

```python
from gridfleet_testkit import get_device_test_data_for_driver

test_data = get_device_test_data_for_driver(driver)
```

### Errors and Result Types

- `CooldownResult`: union response type from `cooldown_device`, with `status` equal to `"cooldown_set"` or `"maintenance_escalated"`. `CooldownSetResult` and `CooldownEscalatedResult` are the concrete TypedDict variants.

## Examples

Baseline screenshot examples:

- `examples/test_android_mobile_screenshot.py`
- `examples/test_android_tv_screenshot.py`
- `examples/test_firetv_screenshot.py`
- `examples/test_ios_simulator_screenshot.py`
- `examples/test_tvos_screenshot.py`
- `examples/test_roku_screenshot.py`

Advanced example:

- `examples/test_roku_sideload_screenshot.py`

The baseline examples share the same flow:

1. Create a session through the WebDriver router
2. Print the resolved connection context
3. Save a screenshot
4. Assert that the screenshot file exists and is non-empty

## Platform Notes

- Android Mobile / Android TV / Fire TV:
  - require the UiAutomator2 driver
  - rely on Grid routing hints generated from GridFleet metadata
- Fire TV:
  - baseline example supports optional `appium:os_version` filtering when you need a specific Fire OS release
- iOS simulator:
  - baseline example intentionally targets the simulator lane with `appium:device_type=simulator`
- tvOS:
  - baseline example intentionally targets a real device and assumes the host already satisfies XCUITest and WebDriverAgent prerequisites
- Roku:
  - screenshot examples install and activate the bundled sample dev app before capture
  - both Roku examples depend on Roku dev credentials
