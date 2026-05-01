# Backend `stop_node`-Releases-Lock Family Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close backend paths that write `Device.availability_status` or `AppiumNode` state after `manager.stop_node` commits and releases the row locks the caller previously held.

**Architecture:** `node_manager_state.mark_node_stopped` (`backend/app/services/node_manager_state.py`) commits before returning. Any caller that writes more device or node state after `manager.stop_node` must immediately re-acquire the relevant row locks, using the documented order: `Device` first, then `AppiumNode`. `maintenance_service.enter_maintenance` needs a post-stop `Device` re-lock; `lifecycle_policy_actions.stop_node_and_mark_offline` needs a post-stop `Device` + `AppiumNode` re-lock in the exception branch; `bulk_service.bulk_enter_maintenance` already locks its initial batch through `_load_devices`, but an intermediate commit inside the first per-device call releases the whole batch, so the loop must re-lock each device just before handing it to `enter_maintenance`.

**Tech Stack:** Python 3.12, SQLAlchemy async, PostgreSQL row locks, FastAPI service layer, `pytest-asyncio`, `uv`.

---

## Files

- Create: `backend/tests/test_concurrency_maintenance_post_stop_node_relock.py`
- Create: `backend/tests/test_concurrency_stop_node_and_mark_offline_except_relock.py`
- Create: `backend/tests/test_concurrency_bulk_enter_maintenance_lock.py`
- Modify: `backend/app/services/maintenance_service.py`
- Modify: `backend/app/services/lifecycle_policy_actions.py`
- Modify: `backend/app/services/bulk_service.py`

The tests below use the current test harness: `db_session_maker`, `db_session`, `db_host`, `tests.helpers.create_device`, and targeted monkeypatching. Do not use `app.db.session`, `register_node_manager`, or `NodeManager` from `node_manager_types`; those APIs do not exist in this repository.

---

### Task 1: Failing regression test for `enter_maintenance` post-stop re-lock

**Files:**
- Create: `backend/tests/test_concurrency_maintenance_post_stop_node_relock.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_concurrency_maintenance_post_stop_node_relock.py`:

```python
import asyncio
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device, DeviceAvailabilityStatus
from app.models.host import Host
from app.services import device_locking, maintenance_service
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


class _StopNodeCommitsManager:
    def __init__(self, stop_committed: asyncio.Event) -> None:
        self._stop_committed = stop_committed

    async def stop_node(self, db: AsyncSession, device: Device) -> AppiumNode:
        assert device.appium_node is not None
        node = device.appium_node
        node.state = NodeState.stopped
        node.pid = None
        device.availability_status = DeviceAvailabilityStatus.offline
        await db.commit()
        self._stop_committed.set()
        return node


async def test_enter_maintenance_relocks_after_stop_node_commit(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maintenance-relock",
        availability_status=DeviceAvailabilityStatus.available,
        verified=True,
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            pid=12345,
            state=NodeState.running,
        )
    )
    await db_session.commit()
    device_id = device.id

    original_lock_device = device_locking.lock_device
    stop_committed = asyncio.Event()
    relock_seen = asyncio.Event()

    async def observed_lock_device(
        db: AsyncSession,
        target_id: uuid.UUID,
        *,
        load_sessions: bool = False,
    ) -> Device:
        locked = await original_lock_device(db, target_id, load_sessions=load_sessions)
        if target_id == device_id and stop_committed.is_set():
            relock_seen.set()
        return locked

    monkeypatch.setattr(device_locking, "lock_device", observed_lock_device)
    manager = _StopNodeCommitsManager(stop_committed)

    async def runner() -> None:
        async with db_session_maker() as session:
            target = await original_lock_device(session, device_id)
            with patch("app.services.maintenance_service.get_node_manager", return_value=manager):
                await maintenance_service.enter_maintenance(session, target, drain=False)

    runner_task = asyncio.create_task(runner())
    try:
        await asyncio.wait_for(stop_committed.wait(), timeout=1)
        await asyncio.wait_for(relock_seen.wait(), timeout=1)
    except asyncio.TimeoutError as exc:
        raise AssertionError("enter_maintenance did not re-lock Device after stop_node committed") from exc
    finally:
        await runner_task

    async with db_session_maker() as verify:
        final_status = (
            await verify.execute(select(Device.availability_status).where(Device.id == device_id))
        ).scalar_one()

    assert final_status == DeviceAvailabilityStatus.maintenance
```

- [ ] **Step 2: Run the new test, expect FAIL**

```bash
cd backend && uv run pytest tests/test_concurrency_maintenance_post_stop_node_relock.py -v
```

Expected: **FAIL** with `AssertionError: enter_maintenance did not re-lock Device after stop_node committed`.

- [ ] **Step 3: Commit the failing test**

```bash
git add backend/tests/test_concurrency_maintenance_post_stop_node_relock.py
git commit -m "test(backend): cover enter_maintenance post-stop_node relock"
```

---

### Task 2: Fix `maintenance_service.enter_maintenance`

**Files:**
- Modify: `backend/app/services/maintenance_service.py`
- Test: `backend/tests/test_concurrency_maintenance_post_stop_node_relock.py`

- [ ] **Step 1: Apply the edit**

In `backend/app/services/maintenance_service.py`, replace the post-`stop_node` assignment with a re-lock before writing maintenance:

```python
    if not drain and device.appium_node and device.appium_node.state == NodeState.running:
        try:
            manager = get_node_manager(device)
            await manager.stop_node(db, device)
            # stop_node commits via mark_node_stopped, releasing our row lock.
            # Re-acquire the Device row before restoring maintenance.
            from app.services import device_locking

            device = await device_locking.lock_device(db, device.id)
            device.availability_status = DeviceAvailabilityStatus.maintenance
        except NodeManagerError as exc:
            logger.warning("Failed to stop node for %s during maintenance: %s", device.id, exc)
```

- [ ] **Step 2: Run the new test, expect PASS**

```bash
cd backend && uv run pytest tests/test_concurrency_maintenance_post_stop_node_relock.py -v
```

Expected: **PASS**.

- [ ] **Step 3: Run related tests**

```bash
cd backend && uv run pytest -q tests/test_maintenance_service.py tests/test_bulk_service.py tests/test_lifecycle_policy.py
```

Expected: all pass.

- [ ] **Step 4: Commit the fix**

```bash
git add backend/app/services/maintenance_service.py
git commit -m "fix(backend): relock device after stop_node in maintenance"
```

---

### Task 3: Failing regression test for `stop_node_and_mark_offline` exception branch

**Files:**
- Create: `backend/tests/test_concurrency_stop_node_and_mark_offline_except_relock.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_concurrency_stop_node_and_mark_offline_except_relock.py`:

```python
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device, DeviceAvailabilityStatus
from app.models.host import Host
from app.services import appium_node_locking, device_locking, lifecycle_policy_actions
from app.services.node_manager_types import NodeManagerError
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


class _StopNodeCommitsThenRaises:
    async def stop_node(self, db: AsyncSession, device: Device) -> AppiumNode:
        assert device.appium_node is not None
        device.availability_status = DeviceAvailabilityStatus.offline
        await db.commit()
        raise NodeManagerError("simulated stop_node failure after commit")


async def test_stop_node_and_mark_offline_relocks_after_stop_node_commit_in_except_branch(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="lpa-except-relock",
        availability_status=DeviceAvailabilityStatus.busy,
        verified=True,
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            pid=12345,
            state=NodeState.running,
        )
    )
    await db_session.commit()
    device_id = device.id

    original_lock_device = device_locking.lock_device
    original_lock_node = appium_node_locking.lock_appium_node_for_device
    device_lock_count = 0
    node_lock_count = 0

    async def observed_lock_device(
        db: AsyncSession,
        target_id: uuid.UUID,
        *,
        load_sessions: bool = False,
    ) -> Device:
        nonlocal device_lock_count
        if target_id == device_id:
            device_lock_count += 1
        return await original_lock_device(db, target_id, load_sessions=load_sessions)

    async def observed_lock_node(db: AsyncSession, target_id: uuid.UUID) -> AppiumNode | None:
        nonlocal node_lock_count
        if target_id == device_id:
            node_lock_count += 1
        return await original_lock_node(db, target_id)

    monkeypatch.setattr(device_locking, "lock_device", observed_lock_device)
    monkeypatch.setattr(appium_node_locking, "lock_appium_node_for_device", observed_lock_node)

    async with db_session_maker() as session:
        target = await session.get(Device, device_id)
        assert target is not None
        await lifecycle_policy_actions.stop_node_and_mark_offline(
            session,
            target,
            source="test",
            reason="simulated failure",
            manager_resolver=lambda _device: _StopNodeCommitsThenRaises(),
        )

    assert device_lock_count >= 2, "Device was not re-locked after stop_node committed and raised"
    assert node_lock_count >= 2, "AppiumNode was not re-locked after stop_node committed and raised"

    async with db_session_maker() as verify:
        final_device = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()
        final_node = (
            await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))
        ).scalar_one()

    assert final_device.availability_status == DeviceAvailabilityStatus.offline
    assert final_node.state == NodeState.error
    assert final_node.pid is None
```

- [ ] **Step 2: Run the new test, expect FAIL**

```bash
cd backend && uv run pytest tests/test_concurrency_stop_node_and_mark_offline_except_relock.py -v
```

Expected: **FAIL** on the `device_lock_count >= 2` assertion.

- [ ] **Step 3: Commit the failing test**

```bash
git add backend/tests/test_concurrency_stop_node_and_mark_offline_except_relock.py
git commit -m "test(backend): cover stop_node_and_mark_offline exception relock"
```

---

### Task 4: Fix `lifecycle_policy_actions.stop_node_and_mark_offline`

**Files:**
- Modify: `backend/app/services/lifecycle_policy_actions.py`
- Test: `backend/tests/test_concurrency_stop_node_and_mark_offline_except_relock.py`

- [ ] **Step 1: Apply the edit**

In `backend/app/services/lifecycle_policy_actions.py`, replace the exception branch inside `stop_node_and_mark_offline` with:

```python
        try:
            manager = manager_resolver(device)
            await manager.stop_node(db, device)
        except Exception:
            # stop_node may commit before raising, releasing both row locks.
            # Re-acquire in the documented Device -> AppiumNode order before
            # writing offline/error state.
            from app.services import appium_node_locking, device_locking

            device = await device_locking.lock_device(db, device.id, load_sessions=True)
            locked_node = await appium_node_locking.lock_appium_node_for_device(db, device.id)
            device.availability_status = DeviceAvailabilityStatus.offline
            if locked_node is not None:
                locked_node.state = NodeState.error
                locked_node.pid = None
            await db.commit()
```

Use `locked_node`, not the pre-commit `node` object, for all post-relock node writes.

- [ ] **Step 2: Run the new test, expect PASS**

```bash
cd backend && uv run pytest tests/test_concurrency_stop_node_and_mark_offline_except_relock.py -v
```

Expected: **PASS**.

- [ ] **Step 3: Run lifecycle-related tests**

```bash
cd backend && uv run pytest -q tests/test_lifecycle_policy.py tests/test_concurrency_lifecycle_node_lock.py
```

Expected: all pass.

- [ ] **Step 4: Commit the fix**

```bash
git add backend/app/services/lifecycle_policy_actions.py
git commit -m "fix(backend): relock device and node in offline exception path"
```

---

### Task 5: Failing regression test for `bulk_enter_maintenance` per-device re-lock

**Files:**
- Create: `backend/tests/test_concurrency_bulk_enter_maintenance_lock.py`

This test deliberately stubs `bulk_service.enter_maintenance` so the first item commits mid-batch, mimicking the real `stop_node` commit. The bulk wrapper must then call `device_locking.lock_device` for each device before each per-device maintenance call, rather than relying on the stale objects returned by the initial `_load_devices` batch lock.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_concurrency_bulk_enter_maintenance_lock.py`:

```python
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.device import Device, DeviceAvailabilityStatus
from app.models.host import Host
from app.services import bulk_service, device_locking
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_bulk_enter_maintenance_relocks_each_device_before_enter_after_intermediate_commit(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await create_device(
        db_session,
        host_id=db_host.id,
        name="bulk-relock-a",
        availability_status=DeviceAvailabilityStatus.available,
        verified=True,
    )
    second = await create_device(
        db_session,
        host_id=db_host.id,
        name="bulk-relock-b",
        availability_status=DeviceAvailabilityStatus.available,
        verified=True,
    )
    await db_session.commit()

    device_ids = [first.id, second.id]
    expected_lock_order = sorted(device_ids)
    original_lock_device = device_locking.lock_device
    lock_device_calls: list[uuid.UUID] = []
    first_enter = True

    async def observed_lock_device(
        db: AsyncSession,
        target_id: uuid.UUID,
        *,
        load_sessions: bool = False,
    ) -> Device:
        locked = await original_lock_device(db, target_id, load_sessions=load_sessions)
        if target_id in device_ids:
            lock_device_calls.append(target_id)
        return locked

    async def fake_enter_maintenance(
        db: AsyncSession,
        device: Device,
        *,
        drain: bool = False,
        commit: bool = True,
        allow_reserved: bool = False,
    ) -> Device:
        nonlocal first_enter
        _ = (drain, allow_reserved)
        assert commit is False
        if first_enter:
            first_enter = False
            await db.commit()
        return device

    monkeypatch.setattr(device_locking, "lock_device", observed_lock_device)
    monkeypatch.setattr(bulk_service, "enter_maintenance", fake_enter_maintenance)

    async with db_session_maker() as session:
        result = await bulk_service.bulk_enter_maintenance(session, device_ids, drain=False)

    assert result == {"total": 2, "succeeded": 2, "failed": 0, "errors": {}}
    assert lock_device_calls == expected_lock_order
```

- [ ] **Step 2: Run the new test, expect FAIL**

```bash
cd backend && uv run pytest tests/test_concurrency_bulk_enter_maintenance_lock.py -v
```

Expected: **FAIL** because `bulk_enter_maintenance` currently relies on the initial `_load_devices` objects and never calls `device_locking.lock_device` per loop iteration.

- [ ] **Step 3: Commit the failing test**

```bash
git add backend/tests/test_concurrency_bulk_enter_maintenance_lock.py
git commit -m "test(backend): cover bulk enter maintenance per-device relock"
```

---

### Task 6: Fix `bulk_service.bulk_enter_maintenance`

**Files:**
- Modify: `backend/app/services/bulk_service.py`
- Test: `backend/tests/test_concurrency_bulk_enter_maintenance_lock.py`

- [ ] **Step 1: Add the import**

In `backend/app/services/bulk_service.py`, add:

```python
from app.services import device_locking
```

- [ ] **Step 2: Replace `bulk_enter_maintenance`**

Replace the full `bulk_enter_maintenance` function with:

```python
async def bulk_enter_maintenance(db: AsyncSession, device_ids: list[uuid.UUID], drain: bool = False) -> dict[str, Any]:
    devices = await _load_devices(db, device_ids)
    ordered_ids = [device.id for device in devices]
    errors: dict[str, str] = {}
    for device_id in ordered_ids:
        try:
            device = await device_locking.lock_device(db, device_id)
            await enter_maintenance(db, device, drain=drain, commit=False)
        except Exception as e:
            errors[str(device_id)] = str(e)
    await db.commit()
    succeeded = len(ordered_ids) - len(errors)
    await event_bus.publish(
        "bulk.operation_completed",
        {
            "operation": "enter_maintenance",
            "total": len(ordered_ids),
            "succeeded": succeeded,
            "failed": len(errors),
        },
    )
    return _result(len(ordered_ids), succeeded, errors)
```

Keep `_load_devices` as-is. It already uses `device_locking.lock_devices`; the new per-device `lock_device` calls are needed because any earlier `enter_maintenance` call can commit inside `manager.stop_node` and release the whole batch lock.

- [ ] **Step 3: Run the new test, expect PASS**

```bash
cd backend && uv run pytest tests/test_concurrency_bulk_enter_maintenance_lock.py -v
```

Expected: **PASS**.

- [ ] **Step 4: Run bulk tests**

```bash
cd backend && uv run pytest -q tests/test_bulk_service.py
```

Expected: all pass.

- [ ] **Step 5: Commit the fix**

```bash
git add backend/app/services/bulk_service.py
git commit -m "fix(backend): relock each device in bulk maintenance loop"
```

---

### Task 7: Backend verification

- [ ] **Step 1: Run the targeted regression set**

```bash
cd backend && uv run pytest -q \
  tests/test_concurrency_maintenance_post_stop_node_relock.py \
  tests/test_concurrency_stop_node_and_mark_offline_except_relock.py \
  tests/test_concurrency_bulk_enter_maintenance_lock.py \
  tests/test_maintenance_service.py \
  tests/test_bulk_service.py \
  tests/test_lifecycle_policy.py \
  tests/test_concurrency_lifecycle_node_lock.py
```

Expected: all pass.

- [ ] **Step 2: Run lint, format check, and mypy**

```bash
cd backend && uv run ruff format --check app/ tests/
cd backend && uv run ruff check app/ tests/
cd backend && uv run mypy app/
```

Expected: all clean.

- [ ] **Step 3: Run the full backend suite**

```bash
cd backend && uv run pytest -q -n auto
```

Expected: all pass. These tests require a working PostgreSQL test database.
