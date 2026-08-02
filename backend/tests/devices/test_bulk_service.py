from __future__ import annotations

import asyncio
import uuid as uuid_module
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import NoResultFound

from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.devices.locking import LockedDevice
    from app.hosts.models import Host

from unittest.mock import MagicMock

from app.appium_nodes.exceptions import NodeManagerError
from app.core.errors import AgentCallError
from app.devices import locking as device_locking
from app.devices.models import (
    Device,
    DeviceEvent,
    DeviceEventType,
    DeviceOperationalState,
)
from app.devices.services import bulk as bulk_service
from app.devices.services.bulk import BulkItemResult, BulkOperationsService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.maintenance import MaintenanceService
from app.devices.services.service import DeviceCrudService
from app.events.models import SystemEvent
from app.jobs.kinds import JOB_KIND_DEVICE_RECOVERY
from app.jobs.models import Job
from app.lifecycle.services.operator_node import (
    OperatorNodeLifecycleService,
    operator_stop_intents,
    operator_stop_sources,
)
from tests.fakes import FakeSessionFactory, FakeSettingsReader
from tests.helpers import create_device


def _mock_db() -> MagicMock:
    db = MagicMock()
    db.bind = object()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _locked_mock_device(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": uuid_module.uuid4(),
        "host_id": uuid_module.uuid4(),
        "appium_node": None,
        "pack_id": "pack",
        "platform_id": "platform",
        "device_type": SimpleNamespace(value="mobile"),
        "connection_type": SimpleNamespace(value="network"),
        "connection_target": "target",
        "ip_address": "10.0.0.2",
        "host": SimpleNamespace(ip="10.0.0.1", agent_port=5100),
    }
    values.update(overrides)
    return SimpleNamespace(device=SimpleNamespace(**values), assert_active=lambda _db: None)


async def test_bulk_start_stop_and_restart_nodes_collect_errors(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    devices = [
        await create_device(
            db_session,
            host_id=db_host.id,
            name="bulk-manager-ok",
            operational_state=DeviceOperationalState.available,
            verified=True,
        ),
        await create_device(
            db_session,
            host_id=db_host.id,
            name="bulk-manager-fail",
            operational_state=DeviceOperationalState.available,
            verified=True,
        ),
    ]
    await db_session.commit()

    async def fake_start_node(_db: AsyncSession, locked: LockedDevice, caller: str, *, operator: object) -> object:
        if locked.device.id == devices[1].id:
            raise NodeManagerError("cannot start")
        return object()

    async def fake_stop_node(_db: AsyncSession, locked: LockedDevice, caller: str, *, operator: object) -> object:
        if locked.device.id == devices[1].id:
            raise RuntimeError("cannot stop")
        return object()

    async def fake_restart_node(_db: AsyncSession, locked: LockedDevice, caller: str, *, operator: object) -> object:
        if locked.device.id == devices[1].id:
            raise NodeManagerError("cannot restart")
        return object()

    monkeypatch.setattr("app.devices.services.bulk._bulk_start_one", fake_start_node)
    monkeypatch.setattr("app.devices.services.bulk._bulk_stop_one", fake_stop_node)
    monkeypatch.setattr("app.devices.services.bulk._bulk_restart_one", fake_restart_node)
    svc = _real_service(db_session_maker, maintenance=MagicMock())
    started = await svc.bulk_start_nodes([device.id for device in devices])
    stopped = await svc.bulk_stop_nodes([device.id for device in devices])
    restarted = await svc.bulk_restart_nodes([device.id for device in devices])

    assert started["succeeded"] == 1
    assert stopped["failed"] == 1
    assert restarted["failed"] == 1


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_bulk_reconnect_filters_ineligible_devices_and_reports_agent_errors(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    eligible_ok = await create_device(
        db_session,
        host_id=db_host.id,
        name="bulk-rc-ok",
        connection_type="network",
        ip_address="10.0.0.20",
        connection_target="10.0.0.20:5555",
        verified=True,
    )
    eligible_fail = await create_device(
        db_session,
        host_id=db_host.id,
        name="bulk-rc-fail",
        connection_type="network",
        ip_address="10.0.0.21",
        connection_target="10.0.0.21:5555",
        verified=True,
    )
    eligible_reports_unsuccessful = await create_device(
        db_session,
        host_id=db_host.id,
        name="bulk-rc-unsuccessful",
        connection_type="network",
        ip_address="10.0.0.22",
        connection_target="10.0.0.22:5555",
        verified=True,
    )
    ineligible = await create_device(
        db_session,
        host_id=db_host.id,
        name="bulk-rc-usb",
        connection_type="usb",
        verified=True,
    )
    # Network-connected but unsupported by its driver pack: a separate gate
    # (_supports_reconnect) from the connection-type check above, and one that
    # must reject before an agent call is ever attempted.
    unsupported_pack = await create_device(
        db_session,
        host_id=db_host.id,
        name="bulk-rc-unsupported-pack",
        pack_id="missing-pack",
        connection_type="network",
        ip_address="10.0.0.23",
        connection_target="10.0.0.23:5555",
        verified=True,
    )
    await db_session.commit()

    outcomes = {
        "10.0.0.20:5555": {"success": True},
        "10.0.0.21:5555": AgentCallError("10.0.0.10", "boom"),
        # No exception: the agent call itself succeeded but reported failure.
        "10.0.0.22:5555": {"success": False},
    }

    async def fake_lifecycle_action(*args: object, **kwargs: object) -> dict[str, object]:
        outcome = outcomes[cast("str", args[2])]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("app.devices.services.bulk.pack_device_lifecycle_action", fake_lifecycle_action)

    _settings_rc = FakeSettingsReader()
    result = await BulkOperationsService(
        publisher=event_bus,
        settings=_settings_rc,
        circuit_breaker=Mock(),
        maintenance=MagicMock(),
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
        operator=OperatorNodeLifecycleService(settings=_settings_rc, publisher=event_bus),
        session_factory=db_session_maker,
    ).bulk_reconnect(
        [
            eligible_ok.id,
            eligible_fail.id,
            eligible_reports_unsuccessful.id,
            ineligible.id,
            unsupported_pack.id,
        ]
    )

    assert result["succeeded"] == 1
    assert result["failed"] == 4
    assert result["errors"][str(ineligible.id)] == "Not a network-connected Android device"
    assert result["errors"][str(eligible_fail.id)] == "boom"
    # The structural-failure branch: no exception, just `{"success": False}`.
    assert result["errors"][str(eligible_reports_unsuccessful.id)] == "Reconnect failed"
    # The driver-pack gate: a different branch (_supports_reconnect) from the
    # connection-type check above, reached before any agent call.
    assert result["errors"][str(unsupported_pack.id)] == "Not a network-connected Android device"


async def test_bulk_delete_and_maintenance_operations_collect_failures(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    devices = [
        await create_device(db_session, host_id=db_host.id, name=f"bulk-collect-{index}", verified=True)
        for index in range(2)
    ]
    await db_session.commit()

    mock_crud = AsyncMock()

    # Key the outcome on the device id, not on call order: items run concurrently,
    # so a positional side_effect list would bind outcomes to whichever id sorted
    # first — a coin flip on random UUIDs.
    async def fake_delete(_db: object, device_id: uuid_module.UUID) -> bool:
        if device_id == devices[1].id:
            raise RuntimeError("cannot delete")
        return False

    mock_crud.delete_device_txn = AsyncMock(side_effect=fake_delete)
    mock_maintenance = MagicMock()
    mock_maintenance.enter_maintenance_locked = AsyncMock(side_effect=[None, RuntimeError("boom")])
    mock_maintenance.exit_maintenance_locked = AsyncMock(side_effect=[ValueError("bad state"), RuntimeError("boom")])
    mock_maintenance.schedule_device_recovery = AsyncMock()

    _settings_del = FakeSettingsReader()
    svc = BulkOperationsService(
        publisher=event_bus,
        settings=_settings_del,
        circuit_breaker=MagicMock(),
        maintenance=mock_maintenance,
        crud=mock_crud,
        operator=OperatorNodeLifecycleService(settings=_settings_del, publisher=event_bus),
        session_factory=db_session_maker,
    )
    # The unknown id is dropped by the pre-filter, so it is not in ``total``.
    deleted = await svc.bulk_delete([devices[0].id, devices[1].id, uuid4()])
    entered = await svc.bulk_enter_maintenance([device.id for device in devices])
    exited = await svc.bulk_exit_maintenance([device.id for device in devices])

    assert deleted == {
        "total": 2,
        "succeeded": 0,
        "failed": 2,
        "errors": {str(devices[0].id): "Device not found", str(devices[1].id): "cannot delete"},
    }
    assert entered["total"] == 2
    assert entered["failed"] == 1
    assert exited["total"] == 2
    assert exited["failed"] == 2


async def test_bulk_exit_maintenance_enqueues_recovery_jobs(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """bulk_exit_maintenance must enqueue exactly one recovery job per successfully-exited device.

    Each device's state mutation commits in its own transaction, and only then is
    its recovery job enqueued — create_job owns that commit, so it can never run
    inside the state mutation and strand a device.
    """
    # Create 3 devices in maintenance.
    devices = [
        await create_device(
            db_session,
            host_id=db_host.id,
            name=f"bulk-exit-recovery-{i}",
            operational_state=DeviceOperationalState.offline,
            lifecycle_policy_state={"maintenance_reason": "Operator entered maintenance"},
        )
        for i in range(3)
    ]
    await db_session.commit()

    result = await _real_service(db_session_maker).bulk_exit_maintenance([d.id for d in devices])

    assert result["succeeded"] == 3
    assert result["failed"] == 0

    # Each successfully-exited device must have exactly one recovery job enqueued.
    async with db_session_maker() as verify:
        rows = (await verify.execute(select(Job).where(Job.kind == JOB_KIND_DEVICE_RECOVERY))).scalars().all()
    assert len(rows) == 3, f"Expected 3 recovery jobs, got {len(rows)}"

    enqueued_device_ids = {row.payload["device_id"] for row in rows}
    expected_device_ids = {str(d.id) for d in devices}
    assert enqueued_device_ids == expected_device_ids, (
        f"Recovery jobs enqueued for wrong device IDs: {enqueued_device_ids!r}"
    )


# ---------------------------------------------------------------------------
# Phase 9: one fresh transaction per device, one summary event, no lock across HTTP
# ---------------------------------------------------------------------------


def _real_service(
    session_factory: object,
    *,
    maintenance: object | None = None,
) -> BulkOperationsService:
    settings = FakeSettingsReader()
    return BulkOperationsService(
        publisher=event_bus,
        settings=settings,
        circuit_breaker=MagicMock(),
        maintenance=maintenance  # type: ignore[arg-type]
        or MaintenanceService(
            settings=settings,
            publisher=event_bus,
            session_factory=session_factory,  # type: ignore[arg-type]
        ),
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
        operator=OperatorNodeLifecycleService(settings=settings, publisher=event_bus),
        session_factory=session_factory,  # type: ignore[arg-type]
    )


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_bulk_enter_maintenance_isolates_one_failed_item(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """A failing item rolls back only its own facts, peers commit, one summary lands.

    The failure is a real aborting statement inside the item transaction, not a
    patched ``side_effect``: only a genuinely aborted transaction exercises the
    rollback path the shared-session version needed a manual ``rollback()`` for.
    """
    healthy = await create_device(
        db_session,
        host_id=db_host.id,
        name="bulk-isolate-ok",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    poisoned = await create_device(
        db_session,
        host_id=db_host.id,
        name="bulk-isolate-fail",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    await db_session.commit()

    class AbortingMaintenance(MaintenanceService):
        async def enter_maintenance_locked(
            self,
            db: AsyncSession,
            locked: LockedDevice,
            *,
            allow_reserved: bool = False,
            maintenance_reason: str = "Operator entered maintenance",
        ) -> None:
            await super().enter_maintenance_locked(
                db, locked, allow_reserved=allow_reserved, maintenance_reason=maintenance_reason
            )
            if locked.device.id == poisoned.id:
                await db.execute(text("SELECT 1 / 0"))

    settings = FakeSettingsReader()
    service = _real_service(
        db_session_maker,
        maintenance=AbortingMaintenance(
            settings=settings,
            publisher=event_bus,
            session_factory=db_session_maker,
        ),
    )

    summary_before = await _summary_event_count(db_session)
    result = await service.bulk_enter_maintenance([healthy.id, poisoned.id])

    assert result["total"] == 2
    assert result["succeeded"] == 1
    assert result["failed"] == 1
    assert str(poisoned.id) in result["errors"]

    async with db_session_maker() as verify:
        rows = {
            device.id: device
            for device in (
                await verify.execute(select(Device).where(Device.id.in_([healthy.id, poisoned.id])))
            ).scalars()
        }
        events = (
            (
                await verify.execute(
                    select(DeviceEvent.device_id).where(
                        DeviceEvent.event_type == DeviceEventType.maintenance_entered,
                        DeviceEvent.device_id.in_([healthy.id, poisoned.id]),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert (rows[healthy.id].lifecycle_policy_state or {}).get("maintenance_reason") is not None
    assert (rows[poisoned.id].lifecycle_policy_state or {}).get("maintenance_reason") is None, (
        "the failed item's fact write survived — its transaction did not roll back"
    )
    assert list(events) == [healthy.id], f"the failed item's device-event row leaked: {events}"
    assert await _summary_event_count(db_session) - summary_before == 1


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_bulk_reconnect_holds_no_session_or_row_lock_across_the_agent_call(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression pin: the read phase closes before ``pack_device_lifecycle_action``.

    ``bulk_reconnect`` used to call ``_load_devices`` (a ``FOR UPDATE`` over every
    requested device) and hold those locks across the whole HTTP fan-out.
    """
    eligible = await create_device(
        db_session,
        host_id=db_host.id,
        name="bulk-reconnect-eligible",
        connection_type="network",
        ip_address="10.0.0.61",
        connection_target="10.0.0.61:5555",
        verified=True,
    )
    ineligible = await create_device(
        db_session,
        host_id=db_host.id,
        name="bulk-reconnect-usb",
        connection_type="usb",
        verified=True,
    )
    await db_session.commit()

    opened: list[AsyncSession] = []
    real_maker = db_session_maker

    class TrackingFactory:
        def __call__(self) -> object:
            return _tracked(real_maker())

        def begin(self) -> object:
            return _tracked(real_maker.begin())

    @asynccontextmanager
    async def _tracked(inner: object) -> AsyncIterator[AsyncSession]:
        async with inner as session:  # type: ignore[attr-defined]
            opened.append(session)
            yield session

    observations: dict[str, object] = {}

    async def fake_lifecycle_action(*args: object, **kwargs: object) -> dict[str, object]:
        observations["sessions_in_transaction"] = [session.in_transaction() for session in opened]
        async with real_maker() as probe:
            try:
                await asyncio.wait_for(device_locking.lock_device(probe, eligible.id), timeout=1.0)
                observations["row_lock_free"] = True
            except TimeoutError:
                observations["row_lock_free"] = False
            finally:
                await probe.rollback()
        return {"success": True}

    monkeypatch.setattr("app.devices.services.bulk.pack_device_lifecycle_action", fake_lifecycle_action)

    result = await _real_service(TrackingFactory()).bulk_reconnect([eligible.id, ineligible.id])

    assert result == {
        "total": 2,
        "succeeded": 1,
        "failed": 1,
        "errors": {str(ineligible.id): "Not a network-connected Android device"},
    }
    assert observations.get("row_lock_free") is True, (
        "bulk_reconnect still held a device row lock while calling the agent"
    )
    assert observations["sessions_in_transaction"] == [False], (
        f"a command session was still in a transaction during the agent call: {observations}"
    )


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_bulk_delete_sorts_and_dedupes_input_ids(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicated ids collapse to one item, and item order is ascending.

    Deliberate response-value change: ``bulk_delete`` used to iterate the raw
    caller list, so a repeated id reported ``total=2, succeeded=1, failed=1``
    with ``"Device not found"`` from the second (already-deleted) pass.
    """
    devices = [
        await create_device(db_session, host_id=db_host.id, name=f"bulk-dedupe-{index}", verified=True)
        for index in range(3)
    ]
    await db_session.commit()

    ordered: list[list[uuid_module.UUID]] = []
    real_filter = bulk_service._load_existing_device_ids

    async def spy(session_factory: object, device_ids: list[uuid_module.UUID]) -> list[uuid_module.UUID]:
        result = await real_filter(session_factory, device_ids)  # type: ignore[arg-type]
        ordered.append(list(result))
        return result

    monkeypatch.setattr(bulk_service, "_load_existing_device_ids", spy)

    service = _real_service(db_session_maker)
    duplicated = await service.bulk_delete([devices[0].id, devices[0].id])
    assert duplicated == {"total": 1, "succeeded": 1, "failed": 0, "errors": {}}

    reversed_ids = [devices[2].id, devices[1].id]
    remaining = await service.bulk_delete([*reversed_ids, reversed_ids[0]])
    assert remaining == {"total": 2, "succeeded": 2, "failed": 0, "errors": {}}
    assert ordered[-1] == sorted(set(reversed_ids)), (
        f"per-item tasks must start in ascending id order, got {ordered[-1]}"
    )


async def test_load_existing_device_ids_returns_empty_without_opening_a_session() -> None:
    assert await bulk_service._load_existing_device_ids(AsyncMock(), []) == []


async def test_node_action_helpers_delegate_to_operator_service() -> None:
    """_bulk_*_one are thin wrappers over operator.request_start/stop/restart."""
    db = _mock_db()
    returned_node = SimpleNamespace(observed_running=True, port=4723)

    mock_operator = SimpleNamespace(
        request_start=AsyncMock(return_value=returned_node),
        request_stop=AsyncMock(return_value=returned_node),
        request_restart=AsyncMock(return_value=returned_node),
    )

    # _bulk_start_one delegates to operator.request_start; the transaction is the
    # orchestrator's responsibility (_run_per_device_action opens one per device).
    node = await bulk_service._bulk_start_one(  # type: ignore[arg-type]
        db, _locked_mock_device(), "operator", operator=mock_operator
    )
    assert node is returned_node
    mock_operator.request_start.assert_awaited_once()
    assert mock_operator.request_start.call_args.kwargs["reason"] == "operator start requested"
    db.commit.assert_not_awaited()

    # _bulk_stop_one raises NodeManagerError when node is None or not running
    with pytest.raises(NodeManagerError, match="No running node"):
        await bulk_service._bulk_stop_one(  # type: ignore[arg-type]
            db, _locked_mock_device(appium_node=None), "operator", operator=mock_operator
        )
    not_running_node = SimpleNamespace(observed_running=False, port=4723)
    with pytest.raises(NodeManagerError, match="No running node"):
        await bulk_service._bulk_stop_one(  # type: ignore[arg-type]
            db, _locked_mock_device(appium_node=not_running_node), "operator", operator=mock_operator
        )

    # _bulk_stop_one delegates to operator.request_stop when node is running
    running_node = SimpleNamespace(observed_running=True, port=4723)
    stopped = await bulk_service._bulk_stop_one(  # type: ignore[arg-type]
        db, _locked_mock_device(appium_node=running_node), "operator", operator=mock_operator
    )
    assert stopped is returned_node
    mock_operator.request_stop.assert_awaited_once()
    assert mock_operator.request_stop.call_args.kwargs["reason"] == "operator stop requested"

    # _bulk_restart_one delegates to operator.request_restart
    restarted = await bulk_service._bulk_restart_one(  # type: ignore[arg-type]
        db,
        _locked_mock_device(appium_node=running_node),
        "operator",
        operator=mock_operator,
    )
    assert restarted is returned_node
    mock_operator.request_restart.assert_awaited_once()
    assert mock_operator.request_restart.call_args.kwargs["reason"] == "operator restart requested"


async def test_bulk_exit_maintenance_schedules_recovery_only_for_the_succeeding_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    success = uuid_module.uuid4()
    failure = uuid_module.uuid4()

    async def fake_load_existing(_factory: object, _device_ids: list[uuid_module.UUID]) -> list[uuid_module.UUID]:
        return [success, failure]

    monkeypatch.setattr(bulk_service, "_load_existing_device_ids", fake_load_existing)
    monkeypatch.setattr(
        device_locking,
        "lock_device_handle",
        AsyncMock(side_effect=lambda _db, device_id, **_: _locked_mock_device(id=device_id)),
    )

    mock_maintenance = MagicMock()
    mock_maintenance.exit_maintenance_locked = AsyncMock(
        side_effect=lambda _db, locked: (
            (_ for _ in ()).throw(ValueError("not in maintenance"))
            if locked.device.id == failure
            else SimpleNamespace(device_id=locked.device.id)
        )
    )
    mock_maintenance.schedule_device_recovery = AsyncMock()

    exited = await _real_service(FakeSessionFactory(_mock_db()), maintenance=mock_maintenance).bulk_exit_maintenance(
        [success, failure]
    )
    assert exited["succeeded"] == 1
    assert exited["errors"][str(failure)] == "not in maintenance"
    # Recovery is owed only by the item whose transaction committed.
    mock_maintenance.schedule_device_recovery.assert_awaited_once_with(success)


def test_bulk_result_helper_builds_the_summary_shape() -> None:
    assert bulk_service._result(3, 2, {"x": "bad"}) == {
        "total": 3,
        "succeeded": 2,
        "failed": 1,
        "errors": {"x": "bad"},
    }


def test_operator_stop_intents_drops_redundant_grid_intent() -> None:
    """P5: operator stop registers only the node hard-stop + recovery deny. The
    node stop already forces accepting_new_sessions=False (node_factor), so the
    operator:stop:grid intent was pure redundancy and has been dropped from both
    the intent set and the revoke sources."""
    device_id = uuid_module.uuid4()
    sources = {intent.source for intent in operator_stop_intents(device_id)}
    assert sources == {
        f"operator:stop:node:{device_id}",
        f"operator:stop:recovery:{device_id}",
    }
    # operator:stop:grid is no longer a revoke target:
    assert f"operator:stop:grid:{device_id}" not in operator_stop_sources(device_id)
    assert operator_stop_sources(device_id) == [
        f"operator:stop:node:{device_id}",
        f"operator:stop:recovery:{device_id}",
    ]


async def test_bulk_per_device_action_records_lock_and_action_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    first = uuid_module.uuid4()
    second = uuid_module.uuid4()
    third = uuid_module.uuid4()

    async def fake_load_existing(_factory: object, _device_ids: list[uuid_module.UUID]) -> list[uuid_module.UUID]:
        return [first, second, third]

    monkeypatch.setattr(bulk_service, "_load_existing_device_ids", fake_load_existing)
    monkeypatch.setattr(
        device_locking,
        "lock_device_handle",
        AsyncMock(side_effect=[NoResultFound, _locked_mock_device(id=second), _locked_mock_device(id=third)]),
    )

    async def action(_session: object, locked: object, _caller: str) -> None:
        if locked.device.id == second:  # type: ignore[attr-defined]
            raise RuntimeError("action failed")

    result = await bulk_service._run_per_device_action(
        FakeSessionFactory(_mock_db()),  # type: ignore[arg-type]
        [first, second, third],
        operation="restart",
        action_fn=action,
        caller="bulk",
        publisher=event_bus,
    )

    assert result["succeeded"] == 1
    assert result["errors"][str(first)] == "Device not found"
    assert result["errors"][str(second)] == "action failed"


def test_bulk_item_result_is_immutable() -> None:
    """Item results cross the gather boundary, so they must be frozen scalars."""
    item = BulkItemResult(uuid_module.uuid4(), None)
    with pytest.raises(AttributeError):
        item.error = "mutated"  # type: ignore[misc]


async def _summary_event_count(db_session: AsyncSession) -> int:
    total = await db_session.scalar(
        select(func.count()).select_from(SystemEvent).where(SystemEvent.type == "bulk.operation_completed")
    )
    return total or 0
