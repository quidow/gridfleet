# CI Integration Guide

This guide shows how to integrate GridFleet's device reservation system with your CI pipelines. The reservation API lets CI jobs:

1. **Reserve** specific devices for a test run
2. **Prepare** devices (install apps, sideload builds) using your existing scripts
3. **Run tests** against the reserved devices through the WebDriver router
4. **Release** devices automatically when done (or on failure/timeout)

## Authentication

When the manager runs with `GRIDFLEET_AUTH_ENABLED=true` (the recommended production setting), every `/api/*` call requires HTTP Basic auth using the manager's machine credentials.

- The `gridfleet_testkit` Python client picks up `GRIDFLEET_TESTKIT_USERNAME` and `GRIDFLEET_TESTKIT_PASSWORD` automatically and sends them on every request.
- Raw `curl` examples need the same credentials passed via `-u`. Set the two env vars once and use them in every API call:

```bash
export GRIDFLEET_TESTKIT_USERNAME="$GRIDFLEET_MACHINE_AUTH_USERNAME"
export GRIDFLEET_TESTKIT_PASSWORD="$GRIDFLEET_MACHINE_AUTH_PASSWORD"
curl -sf -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" \
  -X POST "$GRIDFLEET_URL/api/runs" -H "Content-Type: application/json" -d '...'
```

When auth is disabled (`GRIDFLEET_AUTH_ENABLED=false`), leave the two env vars unset and omit `-u`. The snippets below assume `GRIDFLEET_TESTKIT_USERNAME` / `GRIDFLEET_TESTKIT_PASSWORD` are exported when auth is on; drop the `-u` flag if it is off.

## API Overview

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/runs` | POST | Create a reservation |
| `/api/runs` | GET | List runs (filterable by state) |
| `/api/runs/{id}` | GET | Run detail with reserved devices |
| `/api/runs/{id}/ready` | POST | Compatibility alias that marks the run active |
| `/api/runs/{id}/active` | POST | Signal tests are running |
| `/api/runs/{id}/devices/{device_id}/preparation-failed` | POST | Exclude one reserved device after CI preparation fails |
| `/api/runs/{id}/heartbeat` | POST | Keep-alive ping |
| `/api/runs/{id}/complete` | POST | Mark run as completed |
| `/api/runs/{id}/cancel` | POST | Cancel the run |
| `/api/runs/{id}/force-release` | POST | Admin: force release all devices |

## Reservation Request

```json
{
  "name": "firetv-regression-12345",
  "requirements": [
    {"pack_id": "appium-uiautomator2", "platform_id": "firetv_real", "os_version": "8", "count": 3},
    {"pack_id": "appium-roku-dlenroc", "platform_id": "roku_network", "count": 1, "tags": {"model": "Roku Ultra"}}
  ],
  "ttl_minutes": 60,
  "heartbeat_timeout_sec": 120,
  "created_by": "github-actions/firetv-ci"
}
```

- **requirements**: List of device groups to reserve. Each specifies `pack_id`, `platform_id`, optional os_version/tags, and either an exact `count` or `allocation: "all_available"`.
- **ttl_minutes**: Maximum run duration before auto-expiry (default: 60).
- **heartbeat_timeout_sec**: How long before a missed heartbeat expires the run (default: 120).

When CI should run on every currently eligible matching device, use explicit all-available allocation instead of `count`:

```json
{
  "requirements": [
    {
      "pack_id": "appium-uiautomator2",
      "platform_id": "firetv_real",
      "os_version": "8",
      "allocation": "all_available",
      "min_count": 1
    }
  ]
}
```

`allocation: "all_available"` is evaluated once when the run is created. The response `devices` array is the reserved fleet slice for that run; CI should size its worker count from `devices.length`. `min_count` defaults to `1`, and the manager returns `409` if fewer matching devices are eligible.

Roku is not installed by default; import the curated Roku driver or upload a Roku driver pack before using the Roku example.

`POST /api/runs` is immediate. If matching devices are not currently available, the manager returns `409` instead of waiting inside the request. Use `GET /api/availability` for a quick platform-capacity check or retry later.

## Reservation Response

```json
{
  "id": "uuid",
  "name": "firetv-regression-12345",
  "state": "preparing",
  "devices": [
    {"device_id": "uuid", "identity_value": "G0...", "connection_target": "192.168.1.60:5555", "pack_id": "appium-uiautomator2", "platform_id": "firetv_real", "os_version": "8", "host_ip": "192.168.1.50"}
  ],
  "ttl_minutes": 60,
  "heartbeat_timeout_sec": 120,
  "created_at": "2026-03-27T10:00:00Z"
}
```

## pytest-xdist Routing

When a run reserves multiple devices, pytest-xdist workers create Appium sessions through the run-scoped router endpoint (`GRID_URL/run/{run_id}`). The testkit composes that URL automatically from `GRIDFLEET_RUN_ID`, and the router (via the backend allocation API) admits each session only to a device reserved for that run.

Practical notes:

- size the worker pool from the `devices` array returned by `POST /api/runs`
- keep the run heartbeat active while workers are running
- call `/api/runs/{id}/ready` or `/api/runs/{id}/active` after preparation — this is required; session sync never auto-activates a run, and a run left in `preparing` is eventually expired with the message that `/api/runs/{id}/active` was never signaled. The transition is a lifecycle marker only: run-scoped sessions can run on the run's reserved devices, and are linked to it, from `preparing` onward (preparation sessions show up in Run Detail).
- read the device id from the `appium:gridfleet:deviceId` session capability and fetch config or metadata with `/api/devices/{id}` when a test needs device details
- finish with the normal `complete` or `cancel` call; there is no per-worker release call

## Safety Nets

The GridFleet automatically handles:

- **Heartbeat timeout**: If no heartbeat is received within `heartbeat_timeout_sec`, the run is expired and devices are released.
- **TTL expiry**: If the run exceeds `ttl_minutes`, it's automatically expired.
- **Startup recovery**: On manager restart, any stale runs are detected and expired.

## Handling Preparation Failures Per Device

Preparation is no longer an all-or-nothing step for a reserved run.

If CI discovers that one reserved device failed setup, it can report that exact failure against the reservation instead of cancelling the entire run. The manager will:

- exclude only that device from the run
- preserve the exact CI-supplied message as the exclusion reason
- place the device into `maintenance` and mark it unhealthy for operator visibility
- keep healthy reserved siblings attached to the same run

Use:

```bash
curl -sf -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" \
  -X POST "$GRIDFLEET_URL/api/runs/$RUN_ID/devices/$DEVICE_ID/preparation-failed" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "ADB authorization failed on device during CI setup",
    "source": "ci_preparation"
  }'
```

Request body:

- `message`: required exact failure detail for that device
- `source`: optional string describing the reporter; defaults to `ci_preparation`

Practical notes:

- call this only for devices that are still actively reserved by the run
- the route currently returns `409` for invalid run/device state, including "device is not actively reserved by this run"
- once the remaining healthy devices finish preparation, CI can still call `/api/runs/{id}/ready`
- Run Detail, Devices, and Device Detail will show the exclusion reason so operators can see what failed

## GitHub Actions Examples

### Basic Single-Platform Workflow

```yaml
name: Fire TV Regression Tests
on: [push]

env:
  GRIDFLEET_URL: http://192.168.1.100:8000
  GRID_URL: http://192.168.1.100:4444
  GRIDFLEET_TESTKIT_USERNAME: ${{ secrets.GRIDFLEET_TESTKIT_USERNAME }}
  GRIDFLEET_TESTKIT_PASSWORD: ${{ secrets.GRIDFLEET_TESTKIT_PASSWORD }}

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Reserve devices
        id: reserve
        run: |
          RESPONSE=$(curl -sf -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" \
            -X POST $GRIDFLEET_URL/api/runs \
            -H "Content-Type: application/json" \
            -d '{
              "name": "firetv-regression-${{ github.run_id }}",
              "requirements": [
                {"pack_id": "appium-uiautomator2", "platform_id": "firetv_real", "os_version": "8", "allocation": "all_available", "min_count": 1}
              ],
              "ttl_minutes": 45,
              "created_by": "github/${{ github.workflow }}/${{ github.run_id }}"
            }')
          echo "RUN_ID=$(echo $RESPONSE | jq -r '.id')" >> $GITHUB_ENV
          echo "DEVICES=$(echo $RESPONSE | jq -c '.devices')" >> $GITHUB_ENV
          echo "WORKERS=$(echo $RESPONSE | jq '.devices | length')" >> $GITHUB_ENV
          echo "Reserved $(echo $RESPONSE | jq '.devices | length') devices"

      - name: Start heartbeat
        run: |
          while true; do
            curl -sf -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" \
              -X POST $GRIDFLEET_URL/api/runs/$RUN_ID/heartbeat > /dev/null 2>&1
            sleep 30
          done &
          echo "HEARTBEAT_PID=$!" >> $GITHUB_ENV

      - name: Install apps on devices
        run: |
          echo "$DEVICES" | jq -c '.[]' | while read DEVICE; do
            DEVICE_ID=$(echo "$DEVICE" | jq -r '.device_id')
            TARGET=$(echo "$DEVICE" | jq -r '.connection_target')
            if ! ./scripts/install_apk.sh \
              --device "$TARGET" \
              --apk "s3://builds/myapp-${{ github.sha }}.apk"; then
              curl -sf -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" \
                -X POST "$GRIDFLEET_URL/api/runs/$RUN_ID/devices/$DEVICE_ID/preparation-failed" \
                -H "Content-Type: application/json" \
                -d '{
                  "message": "APK install failed during CI preparation",
                  "source": "github_actions"
                }'
            fi
          done

      - name: Signal ready
        run: curl -sf -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" -X POST $GRIDFLEET_URL/api/runs/$RUN_ID/ready

      - name: Run tests
        env:
          GRID_URL: ${{ env.GRID_URL }}          # router URL, no /api suffix
          GRIDFLEET_API_URL: ${{ env.GRIDFLEET_URL }}/api  # client needs the /api suffix
          GRIDFLEET_RUN_ID: ${{ env.RUN_ID }}    # testkit composes the run-scoped grid URL from this
        run: |
          pytest tests/firetv/ -n $WORKERS  # -n requires pytest-xdist (not a testkit dep)

      - name: Release devices
        if: always()
        run: |
          kill $HEARTBEAT_PID 2>/dev/null || true
          curl -sf -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" -X POST $GRIDFLEET_URL/api/runs/$RUN_ID/complete || \
          curl -sf -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" -X POST $GRIDFLEET_URL/api/runs/$RUN_ID/cancel || true
```

### Multi-Platform Matrix Workflow

```yaml
name: Multi-Platform Test Suite
on: [push]

env:
  GRIDFLEET_URL: http://192.168.1.100:8000
  GRID_URL: http://192.168.1.100:4444
  GRIDFLEET_TESTKIT_USERNAME: ${{ secrets.GRIDFLEET_TESTKIT_USERNAME }}
  GRIDFLEET_TESTKIT_PASSWORD: ${{ secrets.GRIDFLEET_TESTKIT_PASSWORD }}

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        include:
          - platform: firetv
            pack_id: appium-uiautomator2
            platform_id: firetv_real
            os_version: "8"
            count: 3
            prep_script: ./scripts/install_apk.sh
            prep_args: "--apk s3://builds/myapp.apk"
            test_dir: tests/firetv/
            workers: 3
          - platform: tvos
            pack_id: appium-xcuitest
            platform_id: tvos
            count: 2
            prep_script: ./scripts/testflight_install.sh
            prep_args: "--build-number 1234"
            test_dir: tests/tvos/
            workers: 2
          - platform: roku
            pack_id: appium-roku-dlenroc
            platform_id: roku_network
            count: 2
            prep_script: "echo 'No preparation needed for Roku'"
            prep_args: ""
            test_dir: tests/roku/
            workers: 2

    steps:
      - uses: actions/checkout@v4

      - name: Reserve ${{ matrix.platform }} devices
        id: reserve
        run: |
          REQ='[{"pack_id": "${{ matrix.pack_id }}", "platform_id": "${{ matrix.platform_id }}"'
          if [ -n "${{ matrix.os_version }}" ]; then
            REQ="$REQ, \"os_version\": \"${{ matrix.os_version }}\""
          fi
          REQ="$REQ, \"count\": ${{ matrix.count }}}]"

          RESPONSE=$(curl -sf -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" \
            -X POST $GRIDFLEET_URL/api/runs \
            -H "Content-Type: application/json" \
            -d "{
              \"name\": \"${{ matrix.platform }}-${{ github.run_id }}\",
              \"requirements\": $REQ,
              \"ttl_minutes\": 45,
              \"created_by\": \"github/${{ github.run_id }}/${{ matrix.platform }}\"
            }")
          echo "RUN_ID=$(echo $RESPONSE | jq -r '.id')" >> $GITHUB_ENV
          echo "DEVICES=$(echo $RESPONSE | jq -c '.devices')" >> $GITHUB_ENV

      - name: Start heartbeat
        run: |
          while true; do
            curl -sf -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" \
              -X POST $GRIDFLEET_URL/api/runs/$RUN_ID/heartbeat > /dev/null 2>&1
            sleep 30
          done &
          echo "HEARTBEAT_PID=$!" >> $GITHUB_ENV

      - name: Prepare devices
        run: |
          echo "$DEVICES" | jq -c '.[]' | while read DEVICE; do
            DEVICE_ID=$(echo "$DEVICE" | jq -r '.device_id')
            TARGET=$(echo "$DEVICE" | jq -r '.connection_target')
            if ! ${{ matrix.prep_script }} --device "$TARGET" ${{ matrix.prep_args }}; then
              curl -sf -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" \
                -X POST "$GRIDFLEET_URL/api/runs/$RUN_ID/devices/$DEVICE_ID/preparation-failed" \
                -H "Content-Type: application/json" \
                -d "{
                  \"message\": \"${{ matrix.platform }} preparation failed on $TARGET\",
                  \"source\": \"github_actions/${{ matrix.platform }}\"
                }"
            fi
          done

      - name: Signal ready
        run: curl -sf -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" -X POST $GRIDFLEET_URL/api/runs/$RUN_ID/ready

      - name: Run ${{ matrix.platform }} tests
        env:
          GRID_URL: ${{ env.GRID_URL }}          # router URL, no /api suffix
          GRIDFLEET_API_URL: ${{ env.GRIDFLEET_URL }}/api  # client needs the /api suffix
          GRIDFLEET_RUN_ID: ${{ env.RUN_ID }}    # testkit composes the run-scoped grid URL from this
        run: |
          pytest ${{ matrix.test_dir }} -n ${{ matrix.workers }}  # -n requires pytest-xdist (not a testkit dep)

      - name: Release devices
        if: always()
        run: |
          kill $HEARTBEAT_PID 2>/dev/null || true
          curl -sf -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" -X POST $GRIDFLEET_URL/api/runs/$RUN_ID/complete || \
          curl -sf -u "$GRIDFLEET_TESTKIT_USERNAME:$GRIDFLEET_TESTKIT_PASSWORD" -X POST $GRIDFLEET_URL/api/runs/$RUN_ID/cancel || true
```

## Python Client Usage

Install the supported testkit package first:

```bash
uv pip install -e ./testkit
```

The public `GridFleetClient` and cleanup helpers come from `gridfleet_testkit`:

```python
from gridfleet_testkit import GridFleetClient, register_run_cleanup

client = GridFleetClient("http://192.168.1.100:8000/api")

# Reserve devices
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
devices = run["devices"]
worker_count = len(devices)

# Start background heartbeat
heartbeat_thread = client.start_heartbeat(run_id, interval=30)

# Register atexit cleanup: by default this only stops the heartbeat thread; it does
# NOT complete/cancel the run and installs no SIGTERM/SIGINT handlers. Pass
# on_exit="complete" / install_signal_handlers=True to opt into run finalization.
register_run_cleanup(client, run_id, heartbeat_thread)

# ... run your preparation scripts ...
#
# If one reserved device fails setup:
# client.report_preparation_failure(
#     run_id,
#     device_id=device_id,
#     message="Driver bootstrap timed out during CI setup",
#     source="local-dev",
# )
#
# Once the remaining devices are ready:
client.signal_ready(run_id)
client.signal_active(run_id)

# ... run tests ...

# Release devices
heartbeat_thread.stop()
client.complete_run(run_id)
```

For pytest/Appium fixture setup, use:

```python
pytest_plugins = ["gridfleet_testkit.pytest_plugin"]
```
