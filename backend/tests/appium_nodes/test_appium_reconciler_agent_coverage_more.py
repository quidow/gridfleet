import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services import reconciler_agent as node_agent
from app.appium_nodes.services.reconciler_agent import NodeStartDetails
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services.health import DeviceHealthService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.lifecycle.services import remediation_log
from app.lifecycle.services.operator_node import OperatorNodeLifecycleService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device_record
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def _loaded_device(db_session: AsyncSession, db_host: Host, identity: str) -> Device:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value=identity,
        connection_target=identity,
        name=identity,
        operational_state=DeviceOperationalState.available,
    )
    from app.devices.services.service import DeviceCrudService

    loaded = await DeviceCrudService(
        settings=FakeSettingsReader(), identity=DeviceIdentityConflictService(), publisher=event_bus
    ).get_device(db_session, device.id)
    assert loaded is not None
    return loaded


async def test_mark_node_started_rejects_hostless_device_after_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = Device(
        id=__import__("uuid").uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="mark-start-hostless",
        connection_target="mark-start-hostless",
        name="mark-start-hostless",
        os_version="14",
        host_id=None,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    node = AppiumNode(device_id=device.id, port=4723)
    fake_db = MagicMock()
    fake_db.flush = AsyncMock()
    monkeypatch.setattr(
        "app.appium_nodes.services.reconciler_agent._hold_device_row_lock", AsyncMock(return_value=device)
    )
    monkeypatch.setattr(
        "app.appium_nodes.services.reconciler_agent.appium_node_locking.lock_appium_node_for_device",
        AsyncMock(return_value=node),
    )

    with pytest.raises(NodeManagerError, match="no host assigned"):
        await node_agent.mark_node_started(
            fake_db,
            device,
            port=4723,
            pid=123,
            details=NodeStartDetails(allocated_caps={"appium:systemPort": 8200, "custom:flag": "yes"}),
            settings=FakeSettingsReader({}),
            publisher=Mock(),
        )


async def test_start_stop_restart_node_guard_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _svc_settings = FakeSettingsReader({})
    svc = node_agent.ReconcilerAgentService(
        settings=_svc_settings,
        operator=OperatorNodeLifecycleService(
            review=build_review_service(), settings=_svc_settings, publisher=event_bus
        ),
    )
    device = await _loaded_device(db_session, db_host, "start-stop-guards")
    with pytest.raises(NodeManagerError, match="No running node"):
        await svc.stop_node(db_session, device)

    monkeypatch.setattr(
        "app.appium_nodes.services.reconciler_agent.is_ready_for_use_async", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        "app.appium_nodes.services.reconciler_agent.readiness_error_detail_async",
        AsyncMock(return_value="not ready"),
    )
    with pytest.raises(NodeManagerError, match="not ready"):
        await svc.start_node(db_session, device)

    monkeypatch.setattr(
        "app.appium_nodes.services.reconciler_agent.is_ready_for_use_async", AsyncMock(return_value=True)
    )
    node = AppiumNode(device_id=device.id, port=4723, pid=1, active_connection_target="active")
    db_session.add(node)
    await db_session.commit()
    device.appium_node = node
    with pytest.raises(NodeManagerError, match="already running"):
        await svc.start_node(db_session, device)

    restarted = await svc.restart_node(db_session, device)
    assert restarted.restart_requested_at is not None


async def test_wait_for_node_running(monkeypatch: pytest.MonkeyPatch) -> None:
    _wait_settings = FakeSettingsReader({})
    svc = node_agent.ReconcilerAgentService(
        settings=_wait_settings,
        operator=OperatorNodeLifecycleService(
            review=build_review_service(), settings=_wait_settings, publisher=event_bus
        ),
    )
    db = MagicMock()
    db.refresh = AsyncMock()
    db.commit = AsyncMock()
    device_id = uuid.uuid4()

    node_id = uuid.uuid4()
    not_running = AppiumNode(device_id=device_id, port=4725)
    running_node = AppiumNode(device_id=device_id, port=4725, pid=2, active_connection_target="dev")
    db.refresh.reset_mock()
    db.get = AsyncMock(side_effect=[not_running, running_node])
    monkeypatch.setattr(node_agent.asyncio, "sleep", AsyncMock())
    found = await svc.wait_for_node_running(db, node_id, timeout_sec=1, poll_interval_sec=0)
    assert found is running_node
    assert db.get.await_count == 2
    assert db.refresh.await_count == 2

    db.get = AsyncMock(return_value=None)
    assert await svc.wait_for_node_running(db, node_id, timeout_sec=0, poll_interval_sec=0) is None


async def test_mark_node_started_records_non_port_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    device = Device(
        id=uuid.uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="mark-start-caps",
        connection_target="mark-start-caps",
        name="mark-start-caps",
        os_version="14",
        host_id=uuid.uuid4(),
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    monkeypatch.setattr(node_agent, "_hold_device_row_lock", AsyncMock(return_value=device))
    monkeypatch.setattr(
        node_agent.appium_node_locking,
        "lock_appium_node_for_device",
        AsyncMock(return_value=None),
    )
    set_extra = AsyncMock()
    monkeypatch.setattr(node_agent.appium_node_resource_service, "set_node_extra_capability", set_extra)
    monkeypatch.setattr(DeviceHealthService, "apply_node_state_transition", AsyncMock())
    monkeypatch.setattr(node_agent, "reset_reconciler_start_failure_if_needed", AsyncMock(return_value=False))

    node = await node_agent.mark_node_started(
        db,
        device,
        port=4723,
        pid=123,
        details=NodeStartDetails(allocated_caps={"appium:systemPort": 8200, "custom:flag": "yes"}),
        settings=FakeSettingsReader({}),
        publisher=Mock(),
    )

    assert node is device.appium_node
    set_extra.assert_awaited_once_with(db, node_id=node.id, capability_key="custom:flag", value="yes")


async def test_mark_node_started_clears_stale_reconciler_failure(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await _loaded_device(db_session, db_host, "mark-start-clear")
    await remediation_log.append_failure(db_session, device.id, source="appium_reconciler", reason="http_error")
    await db_session.commit()

    monkeypatch.setattr(node_agent, "_hold_device_row_lock", AsyncMock(return_value=device))
    monkeypatch.setattr(
        node_agent.appium_node_locking,
        "lock_appium_node_for_device",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(node_agent.appium_node_resource_service, "set_node_extra_capability", AsyncMock())
    monkeypatch.setattr(DeviceHealthService, "apply_node_state_transition", AsyncMock())

    await node_agent.mark_node_started(
        db_session,
        device,
        port=4723,
        pid=123,
        settings=FakeSettingsReader({}),
        publisher=Mock(),
    )

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    ladder = await remediation_log.load_ladder(db_session, reloaded.id)
    assert ladder.last_failure_source is None
    assert ladder.last_failure_reason is None


async def test_pack_cap_helpers_cover_empty_and_stereotype_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    device = SimpleNamespace(
        pack_id="pack",
        platform_id="android",
        device_type=SimpleNamespace(value="real_device"),
        ip_address="10.0.0.5",
        connection_target="serial",
        identity_value="serial",
        os_version="14",
        device_config={"udid": "serial"},
    )
    monkeypatch.setattr(node_agent, "resolve_pack_for_device", lambda _device: None)
    assert await node_agent._build_appium_default_pack_caps(AsyncMock(), device) == {}

    monkeypatch.setattr(node_agent, "build_appium_driver_caps", lambda _device, **_kwargs: {"platformName": "Android"})
    monkeypatch.setattr(node_agent.appium_capability_keys, "manager_owned_cap_keys", lambda _keys: frozenset())
    monkeypatch.setattr(node_agent, "resolve_pack_for_device", lambda _device: ("pack", "android"))
    monkeypatch.setattr(
        node_agent,
        "render_stereotype",
        AsyncMock(return_value={"appium:automationName": "UiAutomator2"}),
    )
    caps = await node_agent._build_session_aligned_start_caps(AsyncMock(), device, allocated_caps={})

    assert caps["appium:automationName"] == "UiAutomator2"


async def test_start_and_restart_guard_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import patch as mock_patch

    _guard_settings = FakeSettingsReader({})
    svc = node_agent.ReconcilerAgentService(
        settings=_guard_settings,
        operator=OperatorNodeLifecycleService(
            review=build_review_service(), settings=_guard_settings, publisher=event_bus
        ),
    )
    db = MagicMock()
    db.refresh = AsyncMock()
    hostless = SimpleNamespace(id=uuid.uuid4(), host_id=None, appium_node=None)
    monkeypatch.setattr(node_agent, "is_ready_for_use_async", AsyncMock(return_value=True))
    with pytest.raises(NodeManagerError, match="has no host assigned"):
        await svc.start_node(db, hostless)

    start = AsyncMock(return_value="started")
    with mock_patch.object(node_agent.ReconcilerAgentService, "start_node", start):
        result = await svc.restart_node(db, SimpleNamespace(appium_node=None))
    assert result == "started"
    start.assert_awaited_once()
