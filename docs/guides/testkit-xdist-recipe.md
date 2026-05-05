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
from pathlib import Path

import pytest

from gridfleet_testkit import GridFleetClient, register_run_cleanup


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
        config._gridfleet_run = json.loads(RUN_STATE_PATH.read_text())
        config._gridfleet_client = GridFleetClient()
        return

    client = GridFleetClient()
    run = client.reserve_devices(
        name=os.environ.get("GRIDFLEET_RUN_NAME", "ci-run"),
        requirements=json.loads(os.environ["GRIDFLEET_REQUIREMENTS"]),
        ttl_minutes=int(os.environ.get("GRIDFLEET_RUN_TTL_MIN", "60")),
    )
    RUN_STATE_PATH.write_text(json.dumps(run))

    heartbeat = client.start_heartbeat(run["id"])
    register_run_cleanup(client, run["id"], heartbeat)
    client.signal_ready(run["id"])

    config._gridfleet_run = run
    config._gridfleet_client = client


def pytest_collection_finish(session: pytest.Session) -> None:
    config = session.config
    if hasattr(config, "workerinput"):
        return
    config._gridfleet_client.signal_active(config._gridfleet_run["id"])
```

Tune this:

- Prefer `PYTEST_XDIST_TESTRUNUID` for local xdist runs because it is shared across workers.
- Set `GRIDFLEET_RUN_STATE_PATH` explicitly in CI when your launcher controls shared workspace paths.
- The `os.getpid()` fallback prevents non-xdist tempfile collisions; it is not the worker-sharing contract.

## Per-Test Claim And Release

```python
import contextlib

import httpx
import pytest

from gridfleet_testkit import GridFleetClient, hydrate_allocated_device


@contextlib.contextmanager
def claim_for_test(client: GridFleetClient, run_id: str, worker_id: str, *, cooldown_on_error: int = 120):
    claim = client.claim_device_with_retry(run_id, worker_id=worker_id, max_wait_sec=300)
    allocated = hydrate_allocated_device(claim, run_id=run_id, client=client)
    error: Exception | None = None

    try:
        yield allocated
    except Exception as exc:
        error = exc
        raise
    finally:
        if error is not None and _is_device_level(error):
            try:
                client.release_device_with_cooldown(
                    run_id,
                    device_id=allocated.device_id,
                    worker_id=worker_id,
                    reason=type(error).__name__,
                    ttl_seconds=cooldown_on_error,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response is None or exc.response.status_code not in {404, 409}:
                    raise
        else:
            client.release_device_safe(run_id, device_id=allocated.device_id, worker_id=worker_id)


def _is_device_level(exc: Exception) -> bool:
    name = type(exc).__name__
    return name in {"WebDriverException", "InvalidSessionIdException", "NoSuchDriverException"}


@pytest.fixture
def appium_session(request: pytest.FixtureRequest):
    config = request.config
    run = config._gridfleet_run
    client = config._gridfleet_client
    worker_id = getattr(config, "workerinput", {}).get("workerid", "controller")

    with claim_for_test(client, run["id"], worker_id) as allocated:
        yield allocated
```

Tune this:

- `_is_device_level` is project policy. Keep assertion failures as normal releases; use cooldown for Appium/WebDriver/device connectivity failures.
- `cooldown_on_error` is scoped to the active run. Completing or cancelling the run releases physical devices normally.
- `worker_id` may be any string. pytest-xdist workers usually provide `gw0`, `gw1`, and so on.

## Failure Modes

- Controller crash: heartbeat stops and the manager expires the run according to the run heartbeat timeout.
- Worker crash: the claim remains associated with that worker until run cleanup, manual release, cooldown expiry, or run expiry.
- Network partition: claims may fail with retryable no-claim metadata or normal HTTP errors. Keep retry budgets finite.
- Release race: use `release_device_safe(...)` in normal cleanup so run-finalized and already-released states do not hide the original test result.

## xdist Distribution Modes

- `--dist load`: good default for shared device pools.
- `--dist loadgroup`: useful when tests are grouped by driver pack or platform markers.
- `--dist loadfile` / `loadscope`: useful when fixture setup cost dominates and tests in one file or class should share process locality.
- `--dist each`: usually wrong for scarce devices because it replicates the full test suite per worker.

## Smoke Test For The Context Manager

This test validates the claim/release control flow without starting Appium:

```python
from dataclasses import dataclass


@dataclass
class FakeAllocated:
    device_id: str


class FakeClient:
    def __init__(self):
        self.calls = []

    def claim_device_with_retry(self, run_id, *, worker_id, max_wait_sec):
        self.calls.append(("claim", run_id, worker_id, max_wait_sec))
        return {"device_id": "dev-1", "connection_target": "127.0.0.1:4723"}

    def release_device_safe(self, run_id, *, device_id, worker_id):
        self.calls.append(("release_safe", run_id, device_id, worker_id))
        return True


def test_claim_for_test_releases_on_success(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(
        "conftest.hydrate_allocated_device",
        lambda claim, *, run_id, client: FakeAllocated(device_id=claim["device_id"]),
    )

    with claim_for_test(fake, "run-123", "gw0") as allocated:
        assert allocated.device_id == "dev-1"

    assert fake.calls == [
        ("claim", "run-123", "gw0", 300),
        ("release_safe", "run-123", "dev-1", "gw0"),
    ]
```
