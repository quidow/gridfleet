# Runbook: Device IP Ping Health Check

Use this runbook to verify and troubleshoot the IP ping health check, which detects when devices lose Wi-Fi or network connectivity via ICMP probes.

When `GRIDFLEET_AUTH_ENABLED=true`, every `/api/*` call below requires HTTP Basic auth with the manager's machine credentials. Export them once and pass with `-u`:

```bash
export GRIDFLEET_TESTKIT_USERNAME="$GRIDFLEET_MACHINE_AUTH_USERNAME"
export GRIDFLEET_TESTKIT_PASSWORD="$GRIDFLEET_MACHINE_AUTH_PASSWORD"
```

## What IP Ping Does

The IP ping health check runs ICMP ping probes against a device's `ip_address` field. When the device loses Wi-Fi or network connectivity, consecutive failed pings trigger a device flip to offline (via the normal `lifecycle_policy.handle_health_failure` path). The check uses hysteresis — N consecutive misses must occur before the device is declared unhealthy, preventing single transient packet loss from causing flips.

The check only activates when:

- The device has a non-null `ip_address` (operator-set in Settings → Device → IP)
- The device's pack manifest has `device_checks.ip_ping` enabled (opt-in via `applies_when`)
- The device is not in `held` state or `auto_manage=False`

## Configuration Settings

Three settings control IP ping behavior. Adjust them via the Settings UI (Settings → Device Settings):

### `device_checks.ip_ping.consecutive_fail_threshold`

- **Default:** `3`
- **What it does:** Number of consecutive failed pings required to flip the device offline.
- **Use case:** Raise this (e.g., to `5` or `10`) if your Wi-Fi frequently drops frames and causes false offline flips. Lowering it (to `1` or `2`) makes the system more aggressive.

### `device_checks.ip_ping.timeout_sec`

- **Default:** `2.0`
- **Bounds:** `[0.5, 30.0]`
- **What it does:** Timeout in seconds for each ping probe.
- **Use case:** Increase on slow or latency-heavy networks (e.g., to `5.0` or `10.0`) so legitimate pings don't timeout. Keep at `2.0` for most LAN setups.

### `device_checks.ip_ping.count_per_cycle`

- **Default:** `1`
- **What it does:** Number of ICMP echo requests sent per connectivity loop cycle.
- **Use case:** Increase (e.g., to `3`) to average out transient packet loss across multiple probes in one cycle.

## Manual Verification (Smoke Test)

Follow these steps to verify the check works end-to-end:

### Step 1: Start the dev stack

```bash
cd docker && docker compose up --build -d
```

Wait for all services to be healthy (backend, Postgres, frontend). Check the UI at http://localhost:5173.

### Step 2: Seed or prepare an Android USB device

Attach an Android phone via USB to the agent host. Either:

- Use the seeding script: `./scripts/seed_demo.sh` (if available)
- Or manually add the device in the UI: browse to Devices, add a new device, and set its `ip_address` to the device's Wi-Fi IP (you can find this in Android Settings → System → About phone → IP address)

The device must have a reachable IP address on the same network as the backend/agent.

### Step 3: Confirm healthy state

Open the UI and find the device row. The `device_checks_summary` column should show "Healthy" with a green indicator.

Verify the counter is zero by hitting the metrics endpoint:

```bash
curl -s http://localhost:8000/metrics | grep 'gridfleet_ip_ping_consecutive_failures' | head -1
```

Expected output (or similar):

```
gridfleet_ip_ping_consecutive_failures{device_identity="<device_id>",device_platform="android"} 0.0
```

### Step 4: Disconnect the device's Wi-Fi

Go to the phone → Settings → Wi-Fi → turn Wi-Fi **Off** (or select a different network that is unreachable).

Wait for three connectivity loop intervals to pass. The default interval is 60 seconds, so wait approximately **3 minutes**.

### Step 5: Confirm the device flips offline

Return to the UI and check the device row:

- `status` should change from `available` to `offline`
- `device_checks_summary` should mention "ICMP unreachable" or a similar health failure

Verify the counter incremented by hitting metrics:

```bash
curl -s http://localhost:8000/metrics | grep 'gridfleet_ip_ping_consecutive_failures' | head -1
```

Expected output shows `consecutive_failures` > 0 (e.g., `3.0` if `consecutive_fail_threshold=3`).

Also check the total failures counter:

```bash
curl -s http://localhost:8000/metrics | grep 'gridfleet_ip_ping_failures_total'
```

This should show an incrementing count (one increment per connectivity loop cycle while the device is unreachable).

### Step 6: Reconnect Wi-Fi

Go back to the phone → Settings → Wi-Fi → turn Wi-Fi **On** and connect to the network.

Wait approximately one connectivity loop interval (default 60 seconds).

### Step 7: Confirm recovery

The device should return to `available` status automatically via the `attempt_auto_recovery` logic (which was already in place for all health failures). The counter row for this device should disappear from `/metrics`.

Verify:

```bash
curl -s http://localhost:8000/metrics | grep 'gridfleet_ip_ping_consecutive_failures' | head -1
```

Should show `0.0` again (or no row if the device recovered fully).

## Troubleshooting

### Device flips offline overnight or during idle periods

**Cause:** The device's Wi-Fi connection is dropping due to Doze or power saving.

**Fix:**
- Raise `device_checks.ip_ping.consecutive_fail_threshold` to `5` or higher (requires more misses before flipping).
- Or set the device's `auto_manage=False` to exclude it from automatic lifecycle policy enforcement.
- Or disable IP ping entirely for that device by removing it from the pack manifest's `applies_when` rules.

### All USB devices flip offline when you run the smoke test

**Cause:** The agent runtime does not have `ping` available, or the container lacks `cap_net_raw` capability.

**Fix:**
- Verify the `ping` binary exists in the agent runtime image. The Dockerfile should include `iputils-ping` or equivalent.
- Check that the container runs with `--cap-add=NET_RAW` (or equivalent in docker-compose.yml) to allow raw sockets for ICMP.
- Test directly on the agent host: `ping <device_ip> -c 1 -W 5` should succeed if the device is reachable.

### Device stays offline even after Wi-Fi is restored

**Cause:** The `timeout_sec` setting is too low, causing legitimate pings to timeout on a slow network.

**Fix:**
- Increase `device_checks.ip_ping.timeout_sec` to `5.0` or higher (max `30.0`).
- Test manually from the agent host: `ping <device_ip> -c 1 -W 10` (replace 10 with your timeout). If it takes longer than the current setting, increase it.

### Counter never decreases despite device being reachable

**Cause:** The device is pinging successfully, but the counter is not being cleared because a recovery cycle has not completed.

**Fix:**
- Wait one more connectivity loop interval (default 60 seconds).
- Check the device's `ip_address` is set correctly: `curl -s -u ... http://localhost:8000/api/devices/<device_id> | python -m json.tool | grep ip_address`.
- Manually ping from the agent: `docker compose exec agent ping <device_ip> -c 1 -W 5` should succeed if the device is reachable.

## Disabling IP Ping

To disable IP ping checks entirely:

### Option 1: Raise the threshold to an unreachable value

```
Settings → Device Settings → device_checks.ip_ping.consecutive_fail_threshold = 50
```

Devices will rarely or never flip offline due to IP ping failures (set high enough that normal transients never trigger it).

### Option 2: Remove from the pack manifest

Edit the device pack manifest (in `driver-packs/curated/<platform>/<pack>/manifest.yaml`) and remove or comment out the `device_checks.ip_ping` section. Reapply the manifest and refresh the pack on affected devices.

### Option 3: Clear the device's `ip_address`

Open the device in the UI (Settings → Device → IP) and delete the IP address. Without an `ip_address`, the check will not run.
