# Grid Node Capture Fixtures

## Selenium Hub Version

Captured against `selenium/hub:4.41.0`.

## Event Bus Mapping

The capture probe used an isolated throwaway hub on host ports `5542`, `5543`, and `5544` to avoid recording live lab data. The Java relay TOML keys keep the same relative Selenium Grid roles as the production default ports.

| Java TOML key | URL in capture | Socket role observed | Capture mode used |
|---|---|---|---|
| `[events].publish` | `tcp://127.0.0.1:5542` | Hub XPUB endpoint; passive SUB clients receive forwarded Grid events | `observer` |
| `[events].subscribe` | `tcp://127.0.0.1:5543` | Hub XSUB endpoint; passive SUB clients do not receive frames | `tap` required for directional node-to-hub forwarding captures |

## Capture Commands

Use `tests.grid_node.tools.record_grid_bus` for ZMQ and `tests.grid_node.tools.record_grid_http` for Node HTTP transcripts. Raw captures are committed under `raw/<scenario>/`; decoded goldens are committed under `decoded/<scenario>/`.

The socket probe was recorded with a sanitized relay stereotype:

```json
{"platformName":"ANDROID","appium:udid":"fixture-device","appium:platform":"android_fixture"}
```
