# Testkit Integration Guide

This guide shows the supported pattern for building a small downstream pytest plugin on top of `gridfleet_testkit`.

## Thin Claiming Plugin

```python
from __future__ import annotations

from collections.abc import Generator

import pytest
from gridfleet_testkit import GridFleetClient, hydrate_allocated_device


@pytest.fixture(scope="session")
def gridfleet_client() -> GridFleetClient:
    return GridFleetClient()


@pytest.fixture
def allocated_device(gridfleet_client: GridFleetClient, request: pytest.FixtureRequest) -> Generator[object, None, None]:
    run_id = request.config.getoption("--gridfleet-run-id")
    worker_id = getattr(request.config, "workerinput", {}).get("workerid", "local")
    claim = gridfleet_client.claim_device_with_retry(run_id, worker_id=worker_id, max_wait_sec=300)
    allocated = hydrate_allocated_device(claim, run_id=run_id, client=gridfleet_client)
    try:
        yield allocated
    finally:
        gridfleet_client.release_device(run_id, device_id=allocated.device_id, worker_id=worker_id)
```

The plugin owns local fixture naming and worker policy. GridFleet testkit owns API calls, retry metadata, session reporting helpers, and allocated-device hydration.

## Device Listing

```python
from gridfleet_testkit import GridFleetClient

client = GridFleetClient("http://manager-ip:8000/api")
devices = client.list_devices(status="available", platform_id="android_mobile", tags={"team": "qa"})
```

The backend response uses `operational_state` and `hold` for device state. The client sends the filter as `status` because that is the backend query parameter.
