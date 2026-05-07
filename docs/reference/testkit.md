# Testkit Reference

`testkit/` is the supported Python integration surface for external pytest/Appium suites in this repository.

## Support Boundary

- Supported public package: `gridfleet-testkit`
- Supported import root: `gridfleet_testkit`
- Supported pytest plugin: `gridfleet_testkit.pytest_plugin`
- Supported public Appium helpers: `build_appium_options`, `create_appium_driver`
- Supported direct-driver helpers: `get_connection_target_from_driver`, `get_device_config_for_driver`
- Supported public client helpers: `GridFleetClient`, `HeartbeatThread`, `register_run_cleanup`
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
- registers driver-creation setup failures as device-less terminal error sessions, including requested lane metadata and the raw attempted capabilities map
- exposes `device_config` for post-session device-config lookup using live `appium:udid`
- relies on manager-owned session target isolation for driver-sensitive ports and XCUITest build paths on managed nodes

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
| `GridFleetClient.get_device_config(connection_target)` | Look up a device by active connection target, then fetch its config |
| `GridFleetClient.get_driver_pack_catalog()` | Fetch enabled driver-pack catalog data for Appium platform selection |
| `GridFleetClient.reserve_devices(...)` | Create a run/reservation and return the manager response |
| `GridFleetClient.signal_ready(run_id)` | Move a run to `ready` |
| `GridFleetClient.signal_active(run_id)` | Move a run to `active` |
| `GridFleetClient.heartbeat(run_id)` | Send a run heartbeat and read current state |
| `GridFleetClient.report_preparation_failure(run_id, device_id, message, source="ci_preparation")` | Exclude one reserved device after setup fails |
| `GridFleetClient.complete_run(run_id)` | Complete a run |
| `GridFleetClient.cancel_run(run_id)` | Cancel a run |
| `GridFleetClient.start_heartbeat(run_id, interval=30)` | Start a background heartbeat thread |
| `register_run_cleanup(client, run_id, heartbeat_thread=None)` | Register `atexit`/signal cleanup that completes or cancels a run |

Public Appium helpers:

| Helper | Purpose |
| --- | --- |
| `build_appium_options(pack_id, platform_id, capabilities=None, test_name=None)` | Build an Appium options object for an explicit driver-pack platform |
| `create_appium_driver(pack_id, platform_id, capabilities=None, test_name=None, grid_url=GRID_URL)` | Create an Appium remote driver through Selenium Grid for an explicit driver-pack platform |
| `get_connection_target_from_driver(driver)` | Read the active connection target from a live Appium session |
| `get_device_config_for_driver(driver, gridfleet_client=None)` | Fetch device config for a live Appium session using its active connection target |

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
register_run_cleanup(client, run_id, heartbeat_thread)

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

## Reduced HTTP round-trips on claim

`gridfleet-testkit` 0.4.0 lets the manager inline the device config and live capabilities into the claim/reserve response, eliminating per-worker follow-up GETs.

```python
from gridfleet_testkit import GridFleetClient, hydrate_allocated_device

client = GridFleetClient()
claim = client.claim_device(run_id, worker_id="w0", include=("config", "capabilities"))
allocated = hydrate_allocated_device(claim, run_id=run_id, client=client)
# zero follow-up GETs; allocated.config / allocated.live_capabilities populated inline
```

Inline `config` is returned verbatim — all values are present as stored. The manager auth gate (`GRIDFLEET_AUTH_ENABLED`) is the only trust boundary protecting these values.

`reserve_devices` accepts `include=("config",)` only — `include=("capabilities",)` raises `ReserveCapabilitiesUnsupportedError` client-side because reserve-time capabilities are not yet device-bound. Pass `include=` on the per-worker `claim_device` call instead.

`include=` must be a sequence of strings (tuple or list) — order is preserved in the emitted query parameter. Passing a bare string like `include="config"` raises `TypeError` to avoid silently splitting the value into characters.

`hydrate_allocated_device` accepts claim responses only. For multi-device reservations, iterate `reserve_response["devices"]`, call `claim_device` per worker, and hydrate each claim response.

## Examples

Baseline screenshot examples:

- `testkit/examples/test_android_mobile_screenshot.py`
- `testkit/examples/test_android_tv_screenshot.py`
- `testkit/examples/test_firetv_screenshot.py`
- `testkit/examples/test_ios_screenshot.py`
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
