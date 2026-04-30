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
manager-owned base capabilities, manifest default capabilities, manifest `device_fields_schema`
capabilities from `device_config`, user `device_config.appium_caps`, and finally live
parallel-resource allocations. Existing stored `appium_caps` are treated as user overrides.

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
  "appium:derivedDataPath": "/tmp/gridfleet/wda/example-ios-001"
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
  "appium:derivedDataPath": "/tmp/gridfleet/wda/example-ios-sim-001"
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
  "appium:derivedDataPath": "/tmp/gridfleet/wda/example-tvos-001"
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
  "appium:derivedDataPath": "/tmp/gridfleet/wda/example-tvos-sim-001"
}
```

### Roku (real device)

```json
{
  "platformName": "Roku",
  "appium:automationName": "Roku",
  "appium:udid": "192.168.1.80",
  "appium:deviceName": "Roku Ultra",
  "appium:ip": "192.168.1.80",
  "appium:password": "from_device_config"
}
```

Note: `appium:ip` is auto-injected from the device's `connection_target` / network metadata for Roku devices. `appium:password` is auto-injected from the device's `device_config["roku_dev_password"]` if present.

## Manual Example Request Capabilities

The `testkit/examples/` files do not fetch `GET /api/devices/{id}/capabilities` first. They make a request through Selenium Grid using the pytest plugin `pack_id` + `platform_id` selector plus the minimum extra capabilities needed for that lane.

Typical request capabilities for the baseline examples:

| Example lane | Minimal request capabilities |
| --- | --- |
| Android Mobile | `{"appium:platform": "android_mobile", "appium:automationName": "UiAutomator2"}` |
| Android TV | `{"appium:platform": "android_tv", "appium:automationName": "UiAutomator2"}` |
| Fire TV | `{"appium:platform": "firetv_real", "appium:automationName": "UiAutomator2"}` |
| iOS simulator | `{"appium:platform": "ios", "appium:automationName": "XCUITest", "appium:device_type": "simulator"}` |
| tvOS real device | `{"appium:platform": "tvos", "appium:automationName": "XCUITest", "appium:device_type": "real_device"}` |
| Roku | `{"appium:platform": "roku_network", "appium:automationName": "Roku"}` |

`appium:platform` is the Grid stereotype key for the selected pack platform. Run and reservation APIs use `pack_id` plus `platform_id`; raw Appium capabilities use the stereotype generated from the resolved driver platform.

Manual request routing notes:

- `appium:platform` is a Grid stereotype key, not raw Appium `platformName`
- Choose the value from the enabled driver-pack catalog; add it manually if you build options without a testkit shortcut and need Grid routing.
- To target a specific OS release, add `appium:os_version` (Grid stereotypes always emit it from the device column when known).
- If you want raw Appium control instead of the shortcut, omit `platform` and pass `platformName` directly as a normal capability key

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
from gridfleet_testkit import GRIDFLEET_API_URL, GRID_URL

caps = httpx.get(f"{GRIDFLEET_API_URL}/devices/{device_id}/capabilities", timeout=10).json()
caps.update({"appium:app": "/path/to/app.apk"})
driver = webdriver.Remote(GRID_URL, options=UiAutomator2Options().load_capabilities(caps))
```

The supported `gridfleet-testkit` package does not currently wrap this endpoint as a dedicated client helper; fetch it directly from the API when needed.
