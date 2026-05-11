# Testkit Integration Guide

This guide shows the supported pattern for building a small downstream pytest plugin on top of `gridfleet_testkit`.

## Thin Device Metadata Plugin

```python
from __future__ import annotations

from collections.abc import Generator

import pytest
from gridfleet_testkit import GridFleetClient, hydrate_allocated_device, resolve_device_handle_from_driver


@pytest.fixture(scope="session")
def gridfleet_client() -> GridFleetClient:
    return GridFleetClient()


@pytest.fixture
def allocated_device(gridfleet_client: GridFleetClient, request: pytest.FixtureRequest) -> Generator[object, None, None]:
    run_id = request.config.getoption("--gridfleet-run-id")
    driver = request.getfixturevalue("appium_driver")
    device_handle = resolve_device_handle_from_driver(driver, client=gridfleet_client)
    yield hydrate_allocated_device(device_handle, run_id=run_id, client=gridfleet_client)
```

The plugin owns local fixture naming and worker policy. GridFleet testkit owns API calls, Grid-routed capability injection, session reporting helpers, and allocated-device hydration.

## Device Listing

```python
from gridfleet_testkit import GridFleetClient

client = GridFleetClient("http://manager-ip:8000/api")
devices = client.list_devices(status="available", platform_id="android_mobile", tags={"team": "qa"})
```

The backend response uses `operational_state` and `hold` for device state. The client sends the filter as `status` because that is the backend query parameter.
