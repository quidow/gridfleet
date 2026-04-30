# GridFleet E2E Examples

`e2e-examples/` is a standalone consumer project that depends on the local `testkit` the same way an external repository would.

## What It Covers

- Android reservation flow through the GridFleet API
- Run heartbeat lifecycle and cleanup
- Per-device preparation failure reporting
- APK installation with local `adb`
- Real Appium sessions through Selenium Grid for:
  - a native Android app lane using ApiDemos
  - a Chrome browser lane using a `data:` URL

## Required Environment

The orchestration runner expects:

- `GRIDFLEET_URL`
- `GRID_URL`

Optional overrides:

- `GRIDFLEET_API_URL`
- `ANDROID_PACK_ID` default `appium-uiautomator2`
- `ANDROID_PLATFORM` default `android_mobile`
- `ANDROID_DEVICE_COUNT` default `1`
- `ANDROID_APK_URL` default `https://github.com/appium/appium/raw/master/packages/appium/sample-code/apps/ApiDemos-debug.apk`
- `ANDROID_RUN_NAME`
- `ANDROID_CREATED_BY`
- `ANDROID_ADB_PATH` default `adb`
- `ANDROID_PYTEST_ARGS` shell-style pytest argument override
- `ANDROID_JUNIT_XML` optional path for a JUnit XML report

If `GRIDFLEET_API_URL` is omitted, the runner derives it as `${GRIDFLEET_URL}/api`.

## Local Usage

```bash
cd e2e-examples
uv sync --extra dev --extra appium
GRIDFLEET_URL=http://localhost:8000 \
GRID_URL=http://localhost:4444 \
uv run --extra dev --extra appium python -m e2e_examples.run_android_ci
```

The default `pytest` config excludes `e2e_hardware` tests. To run the real-device suite directly:

```bash
cd e2e-examples
uv run --extra dev --extra appium pytest -q -o addopts='' -m e2e_hardware
```

## Android-Capable Hosts

To run the same flow against real Android hardware, the host must already have:

- `adb`
- Python 3.12
- access to the GridFleet API and Selenium Grid URLs
- an Android-capable device or emulator that can satisfy the reservation request
- Chrome available on the reserved Android target for the browser test
