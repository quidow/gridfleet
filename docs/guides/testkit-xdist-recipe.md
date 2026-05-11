# Testkit xdist Recipe

> Reference recipe: adapt this to your project. This is not part of the `gridfleet_testkit` public API.

This recipe shows one pytest-xdist shape for GridFleet runs:

- the controller process creates and owns the run
- the controller owns the heartbeat
- workers read shared run state
- each test claims one device and releases it in a `finally` path
- device-level failures release with cooldown

The policy decisions are intentionally visible. Tune the run-state path, retry budget, cooldown TTL, and device-level error classifier for your test suite.

## Inputs

Set these variables before invoking pytest:

```bash
export GRIDFLEET_API_URL="http://manager-ip:8000/api"
export GRIDFLEET_REQUIREMENTS='[{"pack_id":"appium-uiautomator2","platform_id":"android_mobile","allocation":"all_available","min_count":1}]'
export GRIDFLEET_RUN_NAME="${CI_JOB_NAME:-local-xdist}"
```

`GRIDFLEET_RUN_STATE_PATH` is optional recipe-local glue. Set it when your CI launcher needs a known shared path. It is not exported by `gridfleet_testkit` and is not a supported client contract.

## `conftest.py` Controller And Worker Bootstrap

```python
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest
from pytest import StashKey

from gridfleet_testkit import GridFleetClient, register_run_cleanup

GRIDFLEET_RUN: StashKey[dict] = StashKey()
GRIDFLEET_CLIENT: StashKey[GridFleetClient] = StashKey()
GRIDFLEET_CLEANUP: StashKey[Callable[[], None]] = StashKey()

RUN_STATE_PATH = Path(
    os.environ.get(
        "GRIDFLEET_RUN_STATE_PATH",
        str(
            Path(tempfile.gettempdir())
            / f"gridfleet_run_{os.environ.get('PYTEST_XDIST_TESTRUNUID', os.getpid())}.json"
        ),
    )
)


def pytest_configure(config: pytest.Config) -> None:
    if hasattr(config, "workerinput"):
        config.stash[GRIDFLEET_RUN] = json.loads(RUN_STATE_PATH.read_text())
        config.stash[GRIDFLEET_CLIENT] = GridFleetClient()
        return

    client = GridFleetClient()
    run = client.reserve_devices(
        name=os.environ.get("GRIDFLEET_RUN_NAME", "ci-run"),
        requirements=json.loads(os.environ["GRIDFLEET_REQUIREMENTS"]),
        ttl_minutes=int(os.environ.get("GRIDFLEET_RUN_TTL_MIN", "60")),
    )
    RUN_STATE_PATH.write_text(json.dumps(run))

    heartbeat = client.start_heartbeat(run["id"])
    cleanup = register_run_cleanup(client, run["id"], heartbeat)
    client.signal_ready(run["id"])

    config.stash[GRIDFLEET_RUN] = run
    config.stash[GRIDFLEET_CLIENT] = client
    config.stash[GRIDFLEET_CLEANUP] = cleanup


def pytest_collection_finish(session: pytest.Session) -> None:
    config = session.config
    if hasattr(config, "workerinput"):
        return
    config.stash[GRIDFLEET_CLIENT].signal_active(config.stash[GRIDFLEET_RUN]["id"])


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    config = session.config
    if hasattr(config, "workerinput"):
        return
    run_id = config.stash[GRIDFLEET_RUN]["id"]
    client = config.stash[GRIDFLEET_CLIENT]
    if exitstatus == 0:
        client.complete_run(run_id)
    else:
        client.cancel_run(run_id)
    cleanup = config.stash.get(GRIDFLEET_CLEANUP, None)
    if cleanup is not None:
        cleanup()
```

Tune this:

- Prefer `PYTEST_XDIST_TESTRUNUID` for local xdist runs because it is shared across workers.
- Set `GRIDFLEET_RUN_STATE_PATH` explicitly in CI when your launcher controls shared workspace paths.
- The `os.getpid()` fallback prevents non-xdist tempfile collisions; it is not the worker-sharing contract.
- The controller writes the run-state file once before workers read it. Do not write run state from workers.
- Use `config.stash` with typed `StashKey` instances rather than private `config._...` attributes for all new recipe state. This avoids collisions with pytest internals and is the supported pattern on pytest 7+.
- `register_run_cleanup` returns the cleanup callable. Store it in the stash so `pytest_sessionfinish` can call it after the explicit complete/cancel.
- Explicit `complete_run`/`cancel_run` in `pytest_sessionfinish` is preferred over `on_exit=` because `exitstatus` gives a known outcome. The atexit registered by `register_run_cleanup` uses `on_exit="noop"` by default and only stops the heartbeat thread.

## Per-Test Device Handle

```python
import pytest

from gridfleet_testkit import hydrate_allocated_device, resolve_device_handle_from_driver


@pytest.fixture
def allocated_device(request: pytest.FixtureRequest):
    config = request.config
    run = config.stash[GRIDFLEET_RUN]
    client = config.stash[GRIDFLEET_CLIENT]
    driver = request.getfixturevalue("appium_driver")

    device_handle = resolve_device_handle_from_driver(driver, client=client)
    return hydrate_allocated_device(device_handle, run_id=run["id"], client=client)
```

Tune this:

- The testkit's `appium_driver` fixture injects `gridfleet:run_id`, so Selenium Grid routes the session to a node reserved for the run.
- Resolve device metadata after the session starts, when the driver exposes the runtime connection target.
- Device-level failures should be reported with `report_preparation_failure(...)` before the session starts, or by normal session outcome reporting after the session exists.

## Failure Modes

- Controller crash: heartbeat stops and the manager expires the run according to the run heartbeat timeout.
- Worker crash: any running Grid session ends according to the Grid/Appium failure path; the run remains protected until heartbeat timeout or explicit cleanup.
- Network partition: Appium session creation and manager metadata lookups can fail with normal HTTP/WebDriver errors. Keep retry budgets finite.
- Cleanup race: explicit `complete_run`/`cancel_run` in `pytest_sessionfinish` should tolerate the run already being terminal.

## xdist Distribution Modes

- `--dist load`: good default for shared device pools.
- `--dist loadgroup`: useful when tests are grouped by driver pack or platform markers.
- `--dist loadfile` / `loadscope`: useful when fixture setup cost dominates and tests in one file or class should share process locality.
- `--dist each`: usually wrong for scarce devices because it replicates the full test suite per worker.
