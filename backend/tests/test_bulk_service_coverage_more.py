import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import NoResultFound

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumNode
from app.core.errors import AgentCallError
from app.devices.services import bulk as bulk_service
from app.devices.services.intent_types import GRID_ROUTING, NODE_PROCESS


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


async def test_node_action_helpers_create_stop_and_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db()
    monkeypatch.setattr(bulk_service, "candidate_ports", AsyncMock(return_value=[4723]))
    monkeypatch.setattr(
        bulk_service.settings_service, "get", lambda key: "http://grid" if key == "grid.hub_url" else 30
    )
    revoke = AsyncMock()
    register = AsyncMock()
    monkeypatch.setattr(bulk_service, "revoke_intents_and_reconcile", revoke)
    monkeypatch.setattr(bulk_service, "register_intents_and_reconcile", register)

    with pytest.raises(NodeManagerError, match="no host assigned"):
        await bulk_service._bulk_start_one(db, _device(host_id=None), "operator")

    device = _device()
    node = await bulk_service._bulk_start_one(db, device, "operator")
    assert isinstance(node, AppiumNode)
    assert device.appium_node is node
    assert node.port == 4723
    assert revoke.await_count == 1
    assert register.await_count == 1

    stopped_node = SimpleNamespace(observed_running=True, port=4723)
    stopped = await bulk_service._bulk_stop_one(db, _device(appium_node=stopped_node), "operator")
    assert stopped is stopped_node
    stop_intents = register.await_args.kwargs["intents"]
    assert {intent.axis for intent in stop_intents} == {NODE_PROCESS, GRID_ROUTING}

    restarted_node = SimpleNamespace(observed_running=True, port=4800)
    restarted = await bulk_service._bulk_restart_one(db, _device(appium_node=restarted_node), "operator")
    assert restarted is restarted_node
    restart_intent = register.await_args.kwargs["intents"][0]
    assert restart_intent.payload["desired_port"] == 4800
    assert restart_intent.payload["transition_token"]

    with pytest.raises(NodeManagerError, match="No running node"):
        await bulk_service._bulk_stop_one(db, _device(appium_node=None), "operator")


async def test_bulk_collection_operations_cover_errors_and_non_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _device()
    second = _device()
    db = _db()
    monkeypatch.setattr(bulk_service, "_load_devices", AsyncMock(return_value=[first, second]))
    monkeypatch.setattr(bulk_service, "queue_event_for_session", MagicMock())

    result = await bulk_service.bulk_update_tags(db, [first.id, second.id], {"new": "tag"}, merge=False)
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
    monkeypatch.setattr(bulk_service, "delete_device", fake_delete)
    monkeypatch.setattr(bulk_service.event_bus, "publish", publish)
    deleted = await bulk_service.bulk_delete(db, [first.id, second.id])
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
    monkeypatch.setattr(bulk_service.event_bus, "publish", AsyncMock())

    reconnect = await bulk_service.bulk_reconnect(db, [eligible.id, unsupported.id, failed.id])
    assert reconnect["total"] == 3
    assert reconnect["succeeded"] == 1
    assert reconnect["errors"][str(unsupported.id)] == "Not a network-connected Android device"
    assert reconnect["errors"][str(failed.id)] == "Reconnect failed"

    success = _device()
    failure = _device()
    monkeypatch.setattr(bulk_service, "_load_devices", AsyncMock(return_value=[success, failure]))
    monkeypatch.setattr(
        bulk_service, "exit_maintenance", AsyncMock(side_effect=[None, ValueError("not in maintenance")])
    )
    monkeypatch.setattr(bulk_service, "schedule_device_recovery", AsyncMock(side_effect=RuntimeError("queue down")))
    monkeypatch.setattr(bulk_service, "queue_event_for_session", MagicMock())
    exited = await bulk_service.bulk_exit_maintenance(db, [success.id, failure.id])
    assert exited["succeeded"] == 1
    assert exited["errors"][str(failure.id)] == "not in maintenance"

    async def fake_enter(_db: object, device: object, *, commit: bool) -> None:
        if device is failure:
            raise RuntimeError("enter failed")

    monkeypatch.setattr(bulk_service, "_load_devices", AsyncMock(return_value=[success, failure]))
    monkeypatch.setattr(bulk_service.device_locking, "lock_device", AsyncMock(side_effect=[success, failure]))
    monkeypatch.setattr(bulk_service, "enter_maintenance", fake_enter)
    entered = await bulk_service.bulk_enter_maintenance(db, [success.id, failure.id])
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
    assert bulk_service._operator_stop_sources(device_id) == [
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

    monkeypatch.setattr(bulk_service.event_bus, "publish", AsyncMock())

    result = await bulk_service._run_per_device_node_action(
        db,
        [first, second, third],
        operation="restart",
        action_fn=action,
        caller="bulk",
    )

    assert result["succeeded"] == 1
    assert result["errors"][str(first)] == "Device not found"
    assert result["errors"][str(second)] == "action failed"
