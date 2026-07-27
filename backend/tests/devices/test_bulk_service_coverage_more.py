from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import NoResultFound

from app.appium_nodes.exceptions import NodeManagerError
from app.core.errors import AgentCallError
from app.devices import locking as device_locking
from app.devices.services import bulk as bulk_service
from app.devices.services.bulk import BulkItemResult, BulkOperationsService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from app.lifecycle.services.operator_node import (
    OperatorNodeLifecycleService,
    operator_stop_intents,
    operator_stop_sources,
)
from tests.fakes import FakeSessionFactory, FakeSettingsReader, build_review_service
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host


def _db() -> MagicMock:
    db = MagicMock()
    db.bind = object()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _locked(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "host_id": uuid.uuid4(),
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


def _svc(
    session_factory: object,
    *,
    maintenance: object | None = None,
    crud: object | None = None,
    publisher: object | None = None,
) -> BulkOperationsService:
    settings = FakeSettingsReader({})
    return BulkOperationsService(
        publisher=publisher or event_bus,  # type: ignore[arg-type]
        settings=settings,
        circuit_breaker=MagicMock(),
        maintenance=maintenance or MagicMock(),  # type: ignore[arg-type]
        crud=crud or DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),  # type: ignore[arg-type]
        operator=OperatorNodeLifecycleService(review=build_review_service(), settings=settings, publisher=event_bus),
        session_factory=session_factory,  # type: ignore[arg-type]
    )


async def test_node_action_helpers_delegate_to_operator_service() -> None:
    """_bulk_*_one are thin wrappers over operator.request_start/stop/restart."""
    db = _db()
    returned_node = SimpleNamespace(observed_running=True, port=4723)

    mock_operator = SimpleNamespace(
        request_start=AsyncMock(return_value=returned_node),
        request_stop=AsyncMock(return_value=returned_node),
        request_restart=AsyncMock(return_value=returned_node),
    )

    # _bulk_start_one delegates to operator.request_start; the transaction is the
    # orchestrator's responsibility (_run_per_device_action opens one per device).
    node = await bulk_service._bulk_start_one(db, _locked(), "operator", operator=mock_operator)  # type: ignore[arg-type]
    assert node is returned_node
    mock_operator.request_start.assert_awaited_once()
    assert mock_operator.request_start.call_args.kwargs["reason"] == "operator start requested"
    db.commit.assert_not_awaited()

    # _bulk_stop_one raises NodeManagerError when node is None or not running
    with pytest.raises(NodeManagerError, match="No running node"):
        await bulk_service._bulk_stop_one(db, _locked(appium_node=None), "operator", operator=mock_operator)  # type: ignore[arg-type]
    not_running_node = SimpleNamespace(observed_running=False, port=4723)
    with pytest.raises(NodeManagerError, match="No running node"):
        await bulk_service._bulk_stop_one(  # type: ignore[arg-type]
            db, _locked(appium_node=not_running_node), "operator", operator=mock_operator
        )

    # _bulk_stop_one delegates to operator.request_stop when node is running
    running_node = SimpleNamespace(observed_running=True, port=4723)
    stopped = await bulk_service._bulk_stop_one(  # type: ignore[arg-type]
        db, _locked(appium_node=running_node), "operator", operator=mock_operator
    )
    assert stopped is returned_node
    mock_operator.request_stop.assert_awaited_once()
    assert mock_operator.request_stop.call_args.kwargs["reason"] == "operator stop requested"

    # _bulk_restart_one delegates to operator.request_restart
    restarted = await bulk_service._bulk_restart_one(  # type: ignore[arg-type]
        db,
        _locked(appium_node=running_node),
        "operator",
        operator=mock_operator,
    )
    assert restarted is returned_node
    mock_operator.request_restart.assert_awaited_once()
    assert mock_operator.request_restart.call_args.kwargs["reason"] == "operator restart requested"


async def test_bulk_delete_collects_per_item_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    first = uuid.uuid4()
    second = uuid.uuid4()

    async def fake_load_existing(_factory: object, _device_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        return [first, second]

    monkeypatch.setattr(bulk_service, "_load_existing_device_ids", fake_load_existing)

    delete_results: dict[uuid.UUID, object] = {first: False, second: RuntimeError("delete boom")}

    async def fake_delete(_db: object, device_id: uuid.UUID) -> bool:
        value = delete_results[device_id]
        if isinstance(value, Exception):
            raise value
        return value

    publish = AsyncMock()
    mock_crud = AsyncMock()
    mock_crud.delete_device_txn = AsyncMock(side_effect=fake_delete)

    deleted = await _svc(
        FakeSessionFactory(_db()),
        crud=mock_crud,
        publisher=SimpleNamespace(publish=publish),
    ).bulk_delete([first, second])

    assert deleted["failed"] == 2
    assert deleted["errors"][str(first)] == "Device not found"
    assert deleted["errors"][str(second)] == "delete boom"
    publish.assert_awaited_once()


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_bulk_reconnect_reports_unsupported_and_failed_devices(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eligible = await create_device(
        db_session,
        host_id=db_host.id,
        name="cov-rc-ok",
        connection_type="network",
        ip_address="10.0.0.30",
        connection_target="10.0.0.30:5555",
        verified=True,
    )
    unsupported = await create_device(
        db_session,
        host_id=db_host.id,
        name="cov-rc-unsupported",
        pack_id="missing-pack",
        connection_type="network",
        ip_address="10.0.0.31",
        connection_target="10.0.0.31:5555",
        verified=True,
    )
    failed = await create_device(
        db_session,
        host_id=db_host.id,
        name="cov-rc-failed",
        connection_type="network",
        ip_address="10.0.0.32",
        connection_target="failed-target",
        verified=True,
    )
    await db_session.commit()

    async def fake_lifecycle_action(*args: object, **kwargs: object) -> dict[str, bool]:
        if args[2] == "failed-target":
            return {"success": False}
        return {"success": True}

    monkeypatch.setattr(bulk_service, "pack_device_lifecycle_action", fake_lifecycle_action)

    reconnect = await _svc(db_session_maker).bulk_reconnect([eligible.id, unsupported.id, failed.id])

    assert reconnect["total"] == 3
    assert reconnect["succeeded"] == 1
    assert reconnect["errors"][str(unsupported.id)] == "Not a network-connected Android device"
    assert reconnect["errors"][str(failed.id)] == "Reconnect failed"


async def test_bulk_maintenance_collects_per_item_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    success = uuid.uuid4()
    failure = uuid.uuid4()

    async def fake_load_existing(_factory: object, _device_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        return [success, failure]

    monkeypatch.setattr(bulk_service, "_load_existing_device_ids", fake_load_existing)
    monkeypatch.setattr(
        device_locking,
        "lock_device_handle",
        AsyncMock(side_effect=lambda _db, device_id, **_: _locked(id=device_id)),
    )

    mock_maintenance = MagicMock()
    mock_maintenance.exit_maintenance_locked = AsyncMock(
        side_effect=lambda _db, locked: (
            (_ for _ in ()).throw(ValueError("not in maintenance"))
            if locked.device.id == failure
            else SimpleNamespace(device_id=locked.device.id)
        )
    )
    mock_maintenance.schedule_device_recovery = AsyncMock(side_effect=RuntimeError("queue down"))

    exited = await _svc(FakeSessionFactory(_db()), maintenance=mock_maintenance).bulk_exit_maintenance(
        [success, failure]
    )
    assert exited["succeeded"] == 1
    assert exited["errors"][str(failure)] == "not in maintenance"

    mock_enter = MagicMock()
    mock_enter.enter_maintenance_locked = AsyncMock(
        side_effect=lambda _db, locked, **_: (
            (_ for _ in ()).throw(RuntimeError("enter failed")) if locked.device.id == failure else None
        )
    )
    entered = await _svc(FakeSessionFactory(_db()), maintenance=mock_enter).bulk_enter_maintenance([success, failure])
    assert entered["succeeded"] == 1
    assert entered["errors"][str(failure)] == "enter failed"


def test_bulk_small_helpers_and_errors() -> None:
    device_id = uuid.uuid4()
    assert bulk_service._result(3, 2, {"x": "bad"}) == {
        "total": 3,
        "succeeded": 2,
        "failed": 1,
        "errors": {"x": "bad"},
    }
    assert operator_stop_sources(device_id) == [
        f"operator:stop:node:{device_id}",
        f"operator:stop:recovery:{device_id}",
    ]

    err = AgentCallError("10.0.0.1", "agent down")
    assert str(err) == "agent down"


def test_operator_stop_intents_drops_redundant_grid_intent() -> None:
    """P5: operator stop registers only the node hard-stop + recovery deny. The
    node stop already forces accepting_new_sessions=False (node_factor), so the
    operator:stop:grid intent was pure redundancy and has been dropped from both
    the intent set and the revoke sources."""
    device_id = uuid.uuid4()
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
    first = uuid.uuid4()
    second = uuid.uuid4()
    third = uuid.uuid4()

    async def fake_load_existing(_factory: object, _device_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        return [first, second, third]

    monkeypatch.setattr(bulk_service, "_load_existing_device_ids", fake_load_existing)
    monkeypatch.setattr(
        device_locking,
        "lock_device_handle",
        AsyncMock(side_effect=[NoResultFound, _locked(id=second), _locked(id=third)]),
    )

    async def action(_session: object, locked: object, _caller: str) -> None:
        if locked.device.id == second:  # type: ignore[attr-defined]
            raise RuntimeError("action failed")

    result = await bulk_service._run_per_device_action(
        FakeSessionFactory(_db()),  # type: ignore[arg-type]
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
    item = BulkItemResult(uuid.uuid4(), None)
    with pytest.raises(AttributeError):
        item.error = "mutated"  # type: ignore[misc]
