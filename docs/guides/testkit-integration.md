# Testkit Integration Guide

This guide shows the supported pattern for building a small downstream pytest plugin on top of `gridfleet_testkit`.

## Thin Device Metadata Plugin

```python
from __future__ import annotations

import os
from collections.abc import Generator

import pytest
from gridfleet_testkit import GridFleetClient, hydrate_allocated_device, resolve_device_handle_from_driver


@pytest.fixture(scope="session")
def gridfleet_client() -> GridFleetClient:
    return GridFleetClient()


@pytest.fixture
def allocated_device(gridfleet_client: GridFleetClient, request: pytest.FixtureRequest) -> Generator[object, None, None]:
    # When GRIDFLEET_RUN_ID is set, the appium_driver fixture creates sessions
    # through the run-scoped endpoint (GRID_URL/run/{id}); sessions land only on
    # devices reserved for that run. Without it sessions are free (unreserved
    # devices). Compose the URL manually with run_grid_url(run_id) if you create
    # drivers outside the testkit fixtures.
    run_id = os.environ.get("GRIDFLEET_RUN_ID")
    if run_id is None:
        # hydrate_allocated_device attributes the device to a run (run_id: str);
        # this fixture is for reserved-run suites, so require the run context.
        pytest.skip("allocated_device requires a reserved run (GRIDFLEET_RUN_ID unset)")
    driver = request.getfixturevalue("appium_driver")
    device_handle = resolve_device_handle_from_driver(driver, client=gridfleet_client)
    # resolve_device_handle_from_driver returns the device-detail row keyed by `id`;
    # hydrate_allocated_device requires a `device_id` key, so map it across.
    device_handle["device_id"] = device_handle["id"]
    yield hydrate_allocated_device(device_handle, run_id=run_id, client=gridfleet_client)
```

The plugin owns local fixture naming and worker policy. GridFleet testkit owns API calls, run-scoped grid URL resolution, session reporting helpers, and allocated-device hydration.

## Device Listing

```python
from gridfleet_testkit import GridFleetClient

client = GridFleetClient("http://manager-ip:8000/api")
devices = client.list_devices(status="available", platform_id="android_mobile", tags={"team": "qa"})
```

The backend response uses `operational_state` and the computed `is_reserved` flag (with details in the `reservation` object) for device state. The client sends the filter as `status` because that is the backend query parameter. `status` filters on operational state only — `status="available"` includes devices a run has reserved; pass `reserved=False` alongside it to list only unreserved devices (or `reserved=True` for reserved ones).
