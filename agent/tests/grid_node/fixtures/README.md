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

## HTTP Transcript Capture

To record the Java relay HTTP Node API, start `tests.grid_node.tools.record_grid_http` with `--listen 0.0.0.0:5599` and set the Java relay TOML `server.external-url` to the recorder URL. The recorder forwards to the real Java relay URL and writes `http.transcript` in the selected scenario directory.

## Scenario Fixture Bundles

The scenario bundles under `raw/01_node_bringup` through `raw/08_hard_kill_and_rejoin` are deterministic sanitized fixtures generated with:

```bash
uv run python -m tests.grid_node.tools.generate_grid_fixtures --root tests/grid_node/fixtures
```

The generated raw files intentionally use the same JSONL, multipart-frame base64, and HTTP transcript formats as the live capture tools. The decoded files replace volatile node IDs, session IDs, timestamps, and URIs with placeholders so protocol serializer tests can assert stable shapes without committing lab identifiers.
