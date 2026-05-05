# GridFleet Testkit

`testkit/` is the supported Python integration surface for external pytest/Appium suites that run through GridFleet.

## What This Package Owns

- Stable import root: `gridfleet_testkit`
- Supported pytest plugin: `gridfleet_testkit.pytest_plugin`
- Supported public helpers:
  - `build_appium_options`
  - `create_appium_driver`
  - `get_connection_target_from_driver`
  - `get_device_config_for_driver`
  - `GridFleetClient`
  - `HeartbeatThread`
  - `register_run_cleanup`
- Manual hardware examples under `testkit/examples/`

## What It Does Not Own

- Appium server installation or host-level driver setup
- Selenium Grid lifecycle
- Device registration, verification, or readiness setup
- CI orchestration beyond the documented client helpers

The supported contract is the installable package and documented import pattern. The example scripts are onboarding aids, not CI-backed conformance tests.

## Install

From PyPI:

```bash
pip install "gridfleet-testkit[appium]"
```

From a local checkout:

```bash
uv pip install -e ./testkit[appium]
```

From a copied `testkit/` directory inside another repository:

```bash
uv pip install -e ./testkit[appium]
```

From a Git checkout or VCS URL that contains this package:

```bash
uv pip install "git+https://github.com/<org>/<repo>.git#subdirectory=testkit"
```

The package supports Python 3.10 and newer.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `GRID_URL` | `http://localhost:4444` | Selenium Grid hub URL used by the pytest Appium fixture |
| `GRIDFLEET_API_URL` | `http://localhost:8000/api` | GridFleet API base used for session reporting, config lookup, run helpers, and driver-pack catalog lookup |
| `GRIDFLEET_TESTKIT_USERNAME` | unset | Machine-auth username sent as HTTP Basic auth on every API call. Required when the manager runs with `GRIDFLEET_AUTH_ENABLED=true`. Use the same value as the manager's `GRIDFLEET_MACHINE_AUTH_USERNAME`. |
| `GRIDFLEET_TESTKIT_PASSWORD` | unset | Machine-auth password sent as HTTP Basic auth on every API call. Required when the manager runs with `GRIDFLEET_AUTH_ENABLED=true`. Use the same value as the manager's `GRIDFLEET_MACHINE_AUTH_PASSWORD`. |
| `GRIDFLEET_TESTKIT_PACK_ID` | unset | Optional default driver pack id for Appium option building |
| `GRIDFLEET_TESTKIT_PLATFORM_ID` | unset | Optional default platform id for Appium option building |

The package assumes a running GridFleet API, a reachable Selenium Grid hub, and platform-specific Appium driver setup on the registered hosts. When auth is disabled on the manager, leave `GRIDFLEET_TESTKIT_USERNAME` / `GRIDFLEET_TESTKIT_PASSWORD` unset and the testkit will send no `Authorization` header.

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

### Plugin Lifecycle

- Creates an Appium session through `GRID_URL`
- Injects `gridfleet:testName` with the pytest test name
- Reports final session status back to `GRIDFLEET_API_URL`
- Exposes `device_config` for post-session config lookup using the runtime connection target
- Relies on manager-owned runtime isolation for Appium driver sub-ports and XCUITest build paths

## Direct Appium Usage

If you need to create a driver outside pytest, use the public Appium helpers:

```python
from gridfleet_testkit import create_appium_driver, get_device_config_for_driver

driver = create_appium_driver(
    pack_id="appium-uiautomator2",
    platform_id="firetv_real",
    test_name="manual-smoke",
)

try:
    assert driver.session_id is not None
    device_config = get_device_config_for_driver(driver)
finally:
    driver.quit()
```

`create_appium_driver(...)` reuses the same driver-pack catalog resolver as the pytest fixture. Managed nodes still get their host-scoped runtime allocations from the manager, so callers should not hard-code `systemPort`, `chromedriverPort`, `mjpegServerPort`, `wdaLocalPort`, or `derivedDataPath`. `get_device_config_for_driver(...)` is the non-pytest equivalent of the `device_config` fixture. If you only need the options object, use `build_appium_options(...)`.

## Client Helpers

| Helper | Purpose |
| --- | --- |
| `GridFleetClient.list_devices(filters)` | List devices using backend filters such as `status`, `pack_id`, `platform_id`, `host_id`, `connection_target`, and `tags.*` |
| `GridFleetClient.get_device(device_id)` | Fetch one full device detail row by backend device id |
| `GridFleetClient.get_device_config(connection_target, reveal=True)` | Look up a device by runtime connection target and fetch its config |
| `GridFleetClient.get_driver_pack_catalog()` | Fetch enabled driver-pack catalog data for Appium platform selection |
| `GridFleetClient.reserve_devices(...)` | Create a run/reservation and return the manager response |
| `GridFleetClient.claim_device(run_id, worker_id=...)` | Claim one reserved device for a worker |
| `GridFleetClient.claim_device_with_retry(run_id, worker_id=..., max_wait_sec=300)` | Claim one reserved device, sleeping according to server `Retry-After` responses |
| `GridFleetClient.release_device(run_id, device_id=..., worker_id=...)` | Release a worker claim without cooldown |
| `GridFleetClient.release_device_safe(run_id, device_id=..., worker_id=...)` | Release a worker claim and tolerate 404/409 when cleanup races with run finalization or a prior release |
| `GridFleetClient.release_device_with_cooldown(run_id, device_id=..., worker_id=..., reason=..., ttl_seconds=...)` | Release a worker claim and keep that run from reclaiming the device until cooldown expires |
| `GridFleetClient.signal_ready(run_id)` | Move a run to `ready` |
| `GridFleetClient.signal_active(run_id)` | Move a run to `active` |
| `GridFleetClient.heartbeat(run_id)` | Send a run heartbeat and read current state |
| `GridFleetClient.report_preparation_failure(run_id, device_id, message, source="ci_preparation")` | Exclude one reserved device after setup fails |
| `GridFleetClient.register_session(fields)` | Register a Grid/Appium session with optional requested capability metadata |
| `GridFleetClient.register_session_from_driver(driver, fields)` | Extract session id and capabilities from an Appium driver and register the session |
| `GridFleetClient.update_session_status(session_id, status)` | Report final session status |
| `GridFleetClient.complete_run(run_id)` | Complete a run |
| `GridFleetClient.cancel_run(run_id)` | Cancel a run |
| `GridFleetClient.start_heartbeat(run_id, interval=30)` | Start a background heartbeat thread |
| `build_error_session_payload(fields)` | Build a `/api/sessions` payload for driver-creation failures without importing pytest |
| `hydrate_allocated_device(claim_response, run_id, client)` | Combine a claim response with optional device config and live capabilities |
| `hydrate_allocated_device_from_driver(allocated, driver, client)` | Return a new allocated-device object with capabilities from a running driver |
| `register_run_cleanup(client, run_id, heartbeat_thread=None)` | Register `atexit` and signal cleanup that completes or cancels a run |

### Worker Identity

`worker_id` is an arbitrary string used for claim ownership, telemetry, and cooldown attribution. For pytest-xdist, pass `request.config.workerinput["workerid"]` from worker processes; values are normally `gw0`, `gw1`, and so on. For controller-only flows, use `"controller"` or a stable hostname. For custom schedulers, use a UUID or job-specific worker name.

### Reservation Flow

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

### Worker Claim With Cooldown

```python
from gridfleet_testkit import GridFleetClient

client = GridFleetClient("http://manager-ip:8000/api")

claim = client.claim_device_with_retry(run_id, worker_id="gw0", max_wait_sec=300)
device_id = claim["device_id"]

try:
    # Create the Appium session and run test setup for this worker.
    ...
except RuntimeError as exc:
    client.release_device_with_cooldown(
        run_id,
        device_id=device_id,
        worker_id="gw0",
        reason=str(exc),
        ttl_seconds=60,
    )
    raise
else:
    client.release_device(run_id, device_id=device_id, worker_id="gw0")
```

Cooldowns are scoped to the active run. They prevent the same run from reclaiming the device until `ttl_seconds` expires, but completing or cancelling the run releases the physical device normally.

For pytest-xdist controller/worker orchestration, see [Testkit xdist recipe](../docs/guides/testkit-xdist-recipe.md). The recipe is copyable guidance, not a public testkit abstraction.

### Allocated Device Hydration

Use `hydrate_allocated_device(claim_response, run_id=run_id, client=client)` immediately after a worker claim when a custom plugin needs a stable object instead of raw claim JSON.

```python
from gridfleet_testkit import GridFleetClient, hydrate_allocated_device

client = GridFleetClient("http://manager-ip:8000/api")
run_id = "run-123"
claim = client.claim_device_with_retry(run_id, worker_id="gw0", max_wait_sec=300)
allocated = hydrate_allocated_device(claim, run_id=run_id, client=client)

assert allocated.device_id == claim["device_id"]
assert allocated.platform_name in {"Android", "iOS", "tvOS", "Roku"}
```

The helper fetches static device config by default when `connection_target` is present. It fetches live capabilities only when `fetch_capabilities=True`.

### Reduced HTTP round-trips on claim

`gridfleet-testkit` 0.4.0 lets the manager inline the device config and live capabilities into the claim/reserve response, eliminating per-worker follow-up GETs.

```python
client = GridFleetClient()
claim = client.claim_device(run_id, worker_id="w0", include=("config", "capabilities"))
allocated = hydrate_allocated_device(claim, run_id=run_id, client=client)
# zero follow-up GETs; allocated.config / allocated.live_capabilities populated inline
```

**Masking change**: inline `config` is always masked — sensitive values are replaced with `"********"`. Use `client.get_device_config(connection_target, reveal=True)` if you need raw secrets after the driver session is up. `allocated.config_is_masked` is `True` whenever the inline path was taken.

`reserve_devices` accepts `include=("config",)` only — `include=("capabilities",)` raises `ReserveCapabilitiesUnsupportedError` client-side because reserve-time capabilities are not yet device-bound. Pass `include=` on the per-worker `claim_device` call instead.

`include=` must be a sequence of strings (tuple or list) — order is preserved in the emitted query parameter. Passing a bare string like `include="config"` raises `TypeError` to avoid silently splitting the value into characters.

`hydrate_allocated_device` accepts claim responses only. For multi-device reservations, iterate `reserve_response["devices"]`, call `claim_device` per worker, and hydrate each claim response.

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

1. Create a session through Selenium Grid
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
