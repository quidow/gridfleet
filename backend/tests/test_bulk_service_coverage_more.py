import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import NoResultFound

from app.appium_nodes.exceptions import NodeManagerError
from app.core.errors import AgentCallError
from app.devices.services import bulk as bulk_service
from app.devices.services.bulk import BulkOperationsService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from app.lifecycle.services.operator_node import OperatorNodeLifecycleService, operator_stop_sources
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus


def _db() -> MagicMock:
    db = MagicMock()
    db.bind = object()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _device(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "host_id": uuid.uuid4(),
        "appium_node": None,
        "tags": {"old": "yes"},
        "pack_id": "pack",
        "platform_id": "platform",
        "device_type": SimpleNamespace(value="mobile"),
        "connection_type": SimpleNamespace(value="network"),
        "connection_target": "target",
        "ip_address": "10.0.0.2",
        "host": SimpleNamespace(ip="10.0.0.1", agent_port=5100),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _svc(
    *,
    maintenance: object | None = None,
    settings: object | None = None,
    circuit_breaker: object | None = None,
    operator: object | None = None,
) -> BulkOperationsService:
    _settings = settings or FakeSettingsReader({})
    return BulkOperationsService(
        publisher=event_bus,
        settings=_settings,
        circuit_breaker=circuit_breaker or MagicMock(),
        maintenance=maintenance or MagicMock(),
        crud=DeviceCrudService(settings=_settings, identity=DeviceIdentityConflictService(), publisher=event_bus),
        operator=operator
        or OperatorNodeLifecycleService(review=build_review_service(), settings=_settings, publisher=event_bus),  # type: ignore[arg-type]
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

    # _bulk_start_one delegates to operator.request_start; commit is the orchestrator's
    # responsibility (_run_per_device_node_action._one commits per-device session).
    device = _device()
    node = await bulk_service._bulk_start_one(db, device, "operator", operator=mock_operator)  # type: ignore[arg-type]
    assert node is returned_node
    mock_operator.request_start.assert_awaited_once()
    assert mock_operator.request_start.call_args.kwargs["reason"] == "operator start requested"
    db.commit.assert_not_awaited()

    # _bulk_stop_one raises NodeManagerError when node is None or not running
    with pytest.raises(NodeManagerError, match="No running node"):
        await bulk_service._bulk_stop_one(db, _device(appium_node=None), "operator", operator=mock_operator)  # type: ignore[arg-type]
    not_running_node = SimpleNamespace(observed_running=False, port=4723)
    with pytest.raises(NodeManagerError, match="No running node"):
        await bulk_service._bulk_stop_one(db, _device(appium_node=not_running_node), "operator", operator=mock_operator)  # type: ignore[arg-type]

    # _bulk_stop_one delegates to operator.request_stop when node is running
    running_node = SimpleNamespace(observed_running=True, port=4723)
    stopped = await bulk_service._bulk_stop_one(
        db, _device(appium_node=running_node), "operator", operator=mock_operator
    )  # type: ignore[arg-type]
    assert stopped is returned_node
    mock_operator.request_stop.assert_awaited_once()
    assert mock_operator.request_stop.call_args.kwargs["reason"] == "operator stop requested"

    # _bulk_restart_one delegates to operator.request_restart
    restarted = await bulk_service._bulk_restart_one(
        db,
        _device(appium_node=running_node),
        "operator",
        operator=mock_operator,  # type: ignore[arg-type]
    )
    assert restarted is returned_node
    mock_operator.request_restart.assert_awaited_once()
    assert mock_operator.request_restart.call_args.kwargs["reason"] == "operator restart requested"


async def test_bulk_collection_operations_cover_errors_and_non_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _device()
    second = _device()
    db = _db()
    monkeypatch.setattr(bulk_service, "_load_devices", AsyncMock(return_value=[first, second]))
    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", MagicMock())

    result = await _svc().bulk_update_tags(db, [first.id, second.id], {"new": "tag"}, merge=False)
    assert result == {"total": 2, "succeeded": 2, "failed": 0, "errors": {}}
    assert first.tags == {"new": "tag"}
    assert db.commit.await_count == 1

    delete_calls = {first.id: False, second.id: RuntimeError("delete boom")}

    async def fake_delete(_db: object, device_id: uuid.UUID) -> bool:
        value = delete_calls[device_id]
        if isinstance(value, Exception):
            raise value
        return value

    publish = AsyncMock()
    mock_publisher = SimpleNamespace(publish=publish)
    mock_crud_del = AsyncMock()
    mock_crud_del.delete_device = AsyncMock(side_effect=fake_delete)
    _settings_del2 = FakeSettingsReader({})
    deleted = await BulkOperationsService(
        publisher=mock_publisher,  # type: ignore[arg-type]
        settings=_settings_del2,
        circuit_breaker=MagicMock(),
        maintenance=MagicMock(),
        crud=mock_crud_del,
        operator=OperatorNodeLifecycleService(
            review=build_review_service(), settings=_settings_del2, publisher=event_bus
        ),
    ).bulk_delete(db, [first.id, second.id])
    assert deleted["failed"] == 2
    assert deleted["errors"][str(first.id)] == "Device not found"
    assert deleted["errors"][str(second.id)] == "delete boom"
    publish.assert_awaited()


async def test_bulk_maintenance_and_reconnect_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    eligible = _device()
    unsupported = _device(pack_id="missing", platform_id="platform")
    failed = _device(connection_target="failed-target")
    db = _db()
    monkeypatch.setattr(bulk_service, "_load_devices", AsyncMock(return_value=[eligible, unsupported, failed]))
    monkeypatch.setattr(
        bulk_service,
        "resolve_pack_platform",
        AsyncMock(
            side_effect=[
                SimpleNamespace(lifecycle_actions=[{"id": "reconnect"}]),
                LookupError("missing"),
                SimpleNamespace(lifecycle_actions=[{"id": "reconnect"}]),
            ]
        ),
    )
    monkeypatch.setattr(bulk_service, "platform_has_lifecycle_action", lambda actions, action: bool(actions))

    async def fake_lifecycle_action(*args: object, **kwargs: object) -> dict[str, bool]:
        if args[2] == failed.connection_target:
            return {"success": False}
        return {"success": True}

    monkeypatch.setattr(bulk_service, "pack_device_lifecycle_action", fake_lifecycle_action)
    reconnect = await _svc().bulk_reconnect(
        db,
        [eligible.id, unsupported.id, failed.id],
    )
    assert reconnect["total"] == 3
    assert reconnect["succeeded"] == 1
    assert reconnect["errors"][str(unsupported.id)] == "Not a network-connected Android device"
    assert reconnect["errors"][str(failed.id)] == "Reconnect failed"

    success = _device()
    failure = _device()
    monkeypatch.setattr(bulk_service, "_load_devices", AsyncMock(return_value=[success, failure]))
    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", MagicMock())

    mock_maintenance = MagicMock()
    mock_maintenance.exit_maintenance = AsyncMock(side_effect=[None, ValueError("not in maintenance")])
    mock_maintenance.schedule_device_recovery = AsyncMock(side_effect=RuntimeError("queue down"))
    _settings_exit2 = FakeSettingsReader({})
    exited = await BulkOperationsService(
        publisher=event_bus,
        settings=_settings_exit2,
        circuit_breaker=MagicMock(),
        maintenance=mock_maintenance,
        crud=DeviceCrudService(settings=_settings_exit2, identity=DeviceIdentityConflictService(), publisher=event_bus),
        operator=OperatorNodeLifecycleService(
            review=build_review_service(), settings=_settings_exit2, publisher=event_bus
        ),
    ).bulk_exit_maintenance(db, [success.id, failure.id])
    assert exited["succeeded"] == 1
    assert exited["errors"][str(failure.id)] == "not in maintenance"

    mock_maintenance2 = MagicMock()

    async def fake_enter(_db: object, device: object, *, commit: bool, **kwargs: object) -> object:
        if device is failure:
            raise RuntimeError("enter failed")
        return device

    mock_maintenance2.enter_maintenance = fake_enter
    monkeypatch.setattr(bulk_service, "_load_devices", AsyncMock(return_value=[success, failure]))
    monkeypatch.setattr(bulk_service.device_locking, "lock_device", AsyncMock(side_effect=[success, failure]))
    _settings_enter2 = FakeSettingsReader({})
    entered = await BulkOperationsService(
        publisher=event_bus,
        settings=_settings_enter2,
        circuit_breaker=MagicMock(),
        maintenance=mock_maintenance2,
        crud=DeviceCrudService(
            settings=_settings_enter2, identity=DeviceIdentityConflictService(), publisher=event_bus
        ),
        operator=OperatorNodeLifecycleService(
            review=build_review_service(), settings=_settings_enter2, publisher=event_bus
        ),
    ).bulk_enter_maintenance(db, [success.id, failure.id])
    assert entered["succeeded"] == 1
    assert entered["errors"][str(failure.id)] == "enter failed"


def test_bulk_small_helpers_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock(bind=None)
    with pytest.raises(RuntimeError, match="not bound"):
        bulk_service._session_factory_from_db(db)

    device_id = uuid.uuid4()
    assert bulk_service._result(3, 2, {"x": "bad"}) == {
        "total": 3,
        "succeeded": 2,
        "failed": 1,
        "errors": {"x": "bad"},
    }
    assert operator_stop_sources(device_id) == [
        f"operator:stop:node:{device_id}",
        f"operator:stop:grid:{device_id}",
    ]

    err = AgentCallError("10.0.0.1", "agent down")
    assert str(err) == "agent down"


async def test_bulk_per_device_action_records_lock_and_action_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    first = uuid.uuid4()
    second = uuid.uuid4()
    third = uuid.uuid4()
    db = _db()

    class Session:
        def __init__(self) -> None:
            self.rollback = AsyncMock()
            self.commit = AsyncMock()

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(bulk_service, "_load_existing_device_ids", AsyncMock(return_value=[first, second, third]))
    monkeypatch.setattr(bulk_service, "_session_factory_from_db", lambda _db: Session)
    monkeypatch.setattr(
        bulk_service.device_locking,
        "lock_device",
        AsyncMock(side_effect=[NoResultFound, SimpleNamespace(id=second), SimpleNamespace(id=third)]),
    )

    async def action(_session: object, device: object, _caller: str) -> None:
        if device.id == second:
            raise RuntimeError("action failed")

    result = await bulk_service._run_per_device_node_action(
        db, [first, second, third], operation="restart", action_fn=action, caller="bulk", publisher=event_bus
    )

    assert result["succeeded"] == 1
    assert result["errors"][str(first)] == "Device not found"
    assert result["errors"][str(second)] == "action failed"
