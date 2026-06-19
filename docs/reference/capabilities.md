# Appium Capability Templates

The GridFleet auto-generates Appium capabilities for each device based on its platform, device type, configuration, and live process state. These capabilities are available via the API and displayed on the device detail page.

The product persists `identity_value` and `connection_target`. Android emulators keep a stable host-local AVD identity (`identity_value = avd:<name>`, `connection_target = <name>`), while the Appium-facing `appium:udid` key is derived from the saved connection target or the running node's active target when a session starts; it is not a separate persisted device-registry field.

This page covers two related capability layers:

- manager-generated per-device capabilities returned by `GET /api/devices/{id}/capabilities`
- manual request capabilities used by `gridfleet_testkit.pytest_plugin` and the hardware examples under `testkit/examples/`

Session truth notes:

- `GET /api/devices/{id}/capabilities` is truthful, not predictive
- manager-owned parallel-session resources only appear when the managed Appium node is actually running
- idle devices do not expose speculative future `systemPort`, `chromedriverPort`, `mjpegServerPort`, `wdaLocalPort`, or `derivedDataPath` values

## Override Precedence

When GridFleet builds Appium capabilities, later sources override earlier ones:
manifest default capabilities, manifest `device_fields_schema` capabilities from
`device_config`, user `device_config.appium_caps`, live parallel-resource allocations,
and finally the manager-owned base capabilities (`platformName`, `appium:udid`,
`appium:deviceName`, `appium:gridfleet:deviceId`, `appium:gridfleet:deviceName`), which take
the highest precedence (`appium:automationName` is likewise written last by the builder
when the pack resolves one). Existing stored `appium_caps` are treated as user overrides.

## API Endpoint

```
GET /api/devices/{id}/capabilities
```

Returns a JSON object with the auto-generated capabilities for the device. When the managed Appium node is running, the response also includes the live manager-owned parallel-session allocations for that node.

## Driver Platform Routing

GridFleet routes sessions through driver-pack platforms. Use the enabled catalog to choose a `pack_id` and `platform_id`; curated examples include:

| Driver (`pack_id`) | Platform (`platform_id`) | Appium `platformName` | Default `automationName` |
|--------------------|--------------------------|------------------------|---------------------------|
| `appium-uiautomator2` | `android_mobile` | `Android` | `UiAutomator2` |
| `appium-uiautomator2` | `android_tv` | `Android` | `UiAutomator2` |
| `appium-uiautomator2` | `firetv_real` | `Android` | `UiAutomator2` |
| `appium-xcuitest` | `ios` | `iOS` | `XCUITest` |
| `appium-xcuitest` | `tvos` | `tvOS` | `XCUITest` |

Where a driver supports both real and virtual hardware, the platform id stays the same and `device_type` selects the lane. For example, real iOS devices and iOS simulators both use `platform_id=ios`; real Android TV devices and Android TV emulators both use `platform_id=android_tv`.

## Examples by Platform

### Android Mobile (real device, USB)

```json
{
  "platformName": "Android",
  "appium:automationName": "UiAutomator2",
  "appium:udid": "SERIAL123",
  "appium:deviceName": "Pixel 7",
  "appium:systemPort": 8200,
  "appium:chromedriverPort": 9515,
  "appium:mjpegServerPort": 9200
}
```

### Android TV (real device, network)

```json
{
  "platformName": "Android",
  "appium:automationName": "UiAutomator2",
  "appium:udid": "192.168.1.60:5555",
  "appium:deviceName": "Shield TV",
  "appium:systemPort": 8201,
  "appium:chromedriverPort": 9516,
  "appium:mjpegServerPort": 9201
}
```

### Fire TV (real device)

```json
{
  "platformName": "Android",
  "appium:automationName": "UiAutomator2",
  "appium:udid": "G0...",
  "appium:deviceName": "Fire TV Stick 4K",
  "appium:systemPort": 8202,
  "appium:chromedriverPort": 9517,
  "appium:mjpegServerPort": 9202
}
```

### Android Emulator

```json
{
  "platformName": "Android",
  "appium:automationName": "UiAutomator2",
  "appium:udid": "emulator-5554",
  "appium:deviceName": "Pixel_7_API_34",
  "appium:systemPort": 8203,
  "appium:chromedriverPort": 9518,
  "appium:mjpegServerPort": 9203
}
```

### iOS (real device)

```json
{
  "platformName": "iOS",
  "appium:automationName": "XCUITest",
  "appium:udid": "00008030-...",
  "appium:deviceName": "iPhone 15",
  "appium:platformVersion": "17.4",
  "appium:wdaLocalPort": 8100,
  "appium:mjpegServerPort": 9100,
  "appium:derivedDataPath": "/tmp/gridfleet/derived-data/9f8c2a1b4d6e47f0a1b2c3d4e5f60718"
}
```

### iOS (simulator)

```json
{
  "platformName": "iOS",
  "appium:automationName": "XCUITest",
  "appium:udid": "XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX",
  "appium:deviceName": "iPhone 15 Pro",
  "appium:platformVersion": "17.4",
  "appium:simulatorRunning": true,
  "appium:wdaLocalPort": 8101,
  "appium:mjpegServerPort": 9101,
  "appium:derivedDataPath": "/tmp/gridfleet/derived-data/3b1e7d20c8a94f15b6027e9a4c5d1f83"
}
```

### tvOS (real device, network)

```json
{
  "platformName": "tvOS",
  "appium:automationName": "XCUITest",
  "appium:udid": "00008030-...",
  "appium:deviceName": "Apple TV 4K",
  "appium:platformVersion": "17.4",
  "appium:wdaBaseUrl": "http://192.168.1.70",
  "appium:usePreinstalledWDA": true,
  "appium:updatedWDABundleId": "com.test.WebDriverAgentRunner",
  "appium:wdaLocalPort": 8102,
  "appium:mjpegServerPort": 9102,
  "appium:derivedDataPath": "/tmp/gridfleet/derived-data/c47a9f02e1b84d36a5f0918c2d7e4b6a"
}
```

Note: The Appium process must have `APPIUM_XCUITEST_PREFER_DEVICECTL=1` set in its environment (the GridFleet agent handles this automatically).

### tvOS (simulator)

```json
{
  "platformName": "tvOS",
  "appium:automationName": "XCUITest",
  "appium:udid": "XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX",
  "appium:deviceName": "Apple TV 4K (3rd generation)",
  "appium:platformVersion": "17.4",
  "appium:simulatorRunning": true,
  "appium:wdaLocalPort": 8103,
  "appium:mjpegServerPort": 9103,
  "appium:derivedDataPath": "/tmp/gridfleet/derived-data/7d2f5a08b3c64e91a0e8146f3b9c5d27"
}
```

### Roku (real device)

```json
{
  "platformName": "roku",
  "appium:automationName": "Roku",
  "appium:udid": "192.168.1.80",
  "appium:deviceName": "Roku Ultra",
  "appium:ip": "192.168.1.80",
  "appium:password": "from_device_config"
}
```

Note: `appium:ip` is auto-injected from the device's `connection_target` / network metadata for Roku devices. `appium:password` is auto-injected from the device's `device_config["roku_password"]` if present.

## Manual Example Request Capabilities

The `testkit/examples/` files do not fetch `GET /api/devices/{id}/capabilities` first. They make a request through the WebDriver router using the pytest plugin `pack_id` + `platform_id` selector plus the minimum extra capabilities needed for that lane.

The baseline examples parametrize the `appium_driver` fixture with a `pack_id` + `platform_id` selector (plus an extra cap only where the lane needs one). The testkit derives `appium:platform`, `platformName`, and `appium:automationName` itself; the examples never pass those literally. The selectors used by `testkit/examples/`:

| Example lane | Fixture selector |
| --- | --- |
| Android Mobile | `{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"}` |
| Android TV | `{"pack_id": "appium-uiautomator2", "platform_id": "android_tv"}` |
| Fire TV | `{"pack_id": "appium-uiautomator2", "platform_id": "firetv_real"}` |
| iOS simulator | `{"pack_id": "appium-xcuitest", "platform_id": "ios", "appium:device_type": "simulator"}` |
| tvOS real device | `{"pack_id": "appium-xcuitest", "platform_id": "tvos"}` |
| Roku | `{"pack_id": "appium-roku-dlenroc", "platform_id": "roku_network"}` |

The derived `appium:platform` is the stereotype key for the selected pack platform. Run and reservation APIs use `pack_id` plus `platform_id`; raw Appium capabilities use the stereotype generated from the resolved driver platform.

Manual request routing notes:

- `appium:platform` is a stereotype routing key, not raw Appium `platformName`
- Choose the value from the enabled driver-pack catalog; add it manually if you build options without a testkit shortcut and need the router to match a device.
- To target a specific OS release, add `appium:os_version` (declared by curated pack manifests with a `{device.os_version}` template; the renderer fills it from the device row).
- If you want raw Appium control instead of the shortcut, omit `platform` and pass `platformName` directly as a normal capability key

### Stereotype routing keys

Each device's stereotype is the **routing surface**. The backend allocation API (`/internal/grid/*`, called by the router) matches an incoming new-session request's capabilities against device stereotypes. The stereotype carries only the keys needed to match a client request to a device:

- `platformName`, `appium:automationName` — derived from the active pack/platform manifest.
- `appium:gridfleet:deviceId` — the manager's device UUID, used to round-trip device identity.
- Run routing — sessions bound to a run are created through the run-scoped router endpoint (`/run/{run_id}`); the router extracts the run id from the URL path. Reserved devices admit only sessions from their run; unreserved devices admit only free (non-run) sessions. The legacy `gridfleet:run_id` capability is no longer supported and is rejected with an explicit error.
- `appium:gridfleet:tag:<key>` — one entry per device tag (see the testkit README for tag-based routing).
- Any other keys the pack manifest declares in its `capabilities.stereotype` block. String values support `{device.<attr>}` placeholders, evaluated per device against the live row (e.g. `appium:os_version: "{device.os_version}"`).

Keys that describe the device for Appium's benefit (`appium:ip` and sanitized `device_config.appium_caps`) flow to the Appium driver via the start payload's `extra_caps` field, not via the stereotype. The deprecated `gridfleet:available` sentinel has been removed — `AppiumNode.accepting_new_sessions` covers the routing-suppression cases.

## Manager-Owned Session Caps

These capability keys are manager-owned for managed Appium nodes and are intentionally stripped from operator `device_config["appium_caps"]` overrides:

- `appium:systemPort`
- `appium:chromedriverPort`
- `appium:mjpegServerPort`
- `appium:wdaLocalPort`
- `appium:derivedDataPath`

The manager allocates them per host so concurrent nodes on the same host do not collide.

Non-sensitive Appium overrides, such as `appium:noReset`, `appium:updatedWDABundleId`, or Roku/tvOS setup keys, still remain operator-controlled.

## Overriding Capabilities

The auto-generated capabilities provide the manager-owned session defaults plus operator-safe static capabilities for a session. Tests can still extend them with request-specific values such as `appium:app`:

```python
import httpx
from appium import webdriver
from appium.options.android import UiAutomator2Options
from gridfleet_testkit import api_url, grid_url

caps = httpx.get(f"{api_url()}/devices/{device_id}/capabilities", timeout=10).json()
caps.update({"appium:app": "/path/to/app.apk"})
driver = webdriver.Remote(grid_url(), options=UiAutomator2Options().load_capabilities(caps))
```

The supported `gridfleet-testkit` package does not currently wrap this endpoint as a dedicated client helper; fetch it directly from the API when needed.
