# Runbook: Devices Stuck In Busy Or Reserved

Use this runbook when a device remains `busy` after test traffic ends, or stays `reserved` after the owning run should have released it.

## 1. Inspect the device record first

```bash
curl -s http://localhost:8000/api/devices/DEVICE_ID | python -m json.tool
curl -s http://localhost:8000/api/devices/DEVICE_ID/health | python -m json.tool
curl -s 'http://localhost:8000/api/devices/DEVICE_ID/logs?lines=200' | python -m json.tool
```

Focus on:

- `status`
- `reservation`
- `health_summary`
- lifecycle summary state
- recent Appium logs

## 2. If the device is `reserved`, recover the owning run instead of the device

The device payload includes a `reservation` object with `run_id` and `run_name`.

```bash
curl -s http://localhost:8000/api/runs/RUN_ID | python -m json.tool
```

Use the run timestamps and `last_heartbeat` to decide between normal cancel and force release.

Normal cancel:

```bash
curl -X POST http://localhost:8000/api/runs/RUN_ID/cancel | python -m json.tool
```

Administrative break-glass release:

```bash
curl -X POST http://localhost:8000/api/runs/RUN_ID/force-release | python -m json.tool
```

Do not edit the database directly to clear reservations. The run APIs are the supported recovery path.

## 3. If the device is `busy` but not reserved, check whether the node or session is stale

```bash
curl -s 'http://localhost:8000/api/sessions?limit=20' | python -m json.tool
curl -X POST http://localhost:8000/api/devices/DEVICE_ID/node/restart | python -m json.tool
```

Use node restart when:

- the device is not reserved
- the Appium node is stuck or unhealthy
- logs show the node stopped reporting progress

## 4. If the device is in maintenance or lifecycle suppression, clear the real blocker

Exit maintenance:

```bash
curl -X POST http://localhost:8000/api/devices/DEVICE_ID/maintenance/exit | python -m json.tool
```

Reconnect a supported network Android / Fire TV device:

```bash
curl -X POST http://localhost:8000/api/devices/DEVICE_ID/reconnect | python -m json.tool
```

## 5. Verify that the device returned to service

```bash
curl -s http://localhost:8000/api/devices/DEVICE_ID | python -m json.tool
curl -s 'http://localhost:8000/api/devices?status=reserved' | python -m json.tool
curl -s 'http://localhost:8000/api/devices?status=busy' | python -m json.tool
```

If the device immediately becomes stuck again, capture backend and agent logs before retrying the same recovery action repeatedly.
