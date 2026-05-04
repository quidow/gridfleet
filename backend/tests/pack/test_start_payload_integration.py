import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import ConnectionType, Device, DeviceType
from app.models.host import Host, HostStatus, OSType
from app.services import node_service
from app.services.node_service import build_agent_start_payload
from app.services.pack_capability_service import render_stereotype
from app.services.pack_start_shim import PackStartPayloadError, build_pack_start_payload
from tests.pack.factories import seed_test_packs


class _FakeHost:
    ip = "127.0.0.1"
    agent_port = 5100


class _FakeHttpxResponse:
    """Minimal shim for httpx.Response used by node_service.

    `response_json_dict(resp)` calls `resp.json()`; caller also calls
    `resp.raise_for_status()` in both start and restart paths.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


@pytest.fixture
def _android_real_device() -> MagicMock:
    """Minimal Device-like mock carrying the attributes the start payload builder reads."""
    device: MagicMock = MagicMock()
    device.id = "00000000-0000-0000-0000-000000000099"
    device.pack_id = "appium-uiautomator2"
    device.platform_id = "android_mobile"
    device.device_type = DeviceType.real_device
    device.connection_type = MagicMock(value="usb")
    device.ip_address = None
    device.name = "gate-pixel"
    device.model = "Pixel 6"
    device.manufacturer = "Google"
    device.os_version = "14"
    device.tags = {}
    return device


@pytest.fixture
def _patched_remote_start(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def _fake_appium_start(
        agent_base: str, *, host: str, payload: dict[str, Any], **kwargs: Any
    ) -> _FakeHttpxResponse:
        captured["payload"] = payload
        return _FakeHttpxResponse(
            {
                "port": payload["port"],
                "pid": 1,
                "connection_target": payload["connection_target"],
            }
        )

    async def _noop_ready(*args: Any, **kwargs: Any) -> None:
        return None

    async def _noop_session_aligned(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(node_service, "appium_start", _fake_appium_start)
    monkeypatch.setattr(node_service, "_wait_for_remote_appium_ready", _noop_ready)
    monkeypatch.setattr(node_service, "_build_session_aligned_start_caps", _noop_session_aligned)
    return captured


@pytest.mark.asyncio
async def test_temporary_start_merges_pack_stereotype_over_legacy_caps(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    _patched_remote_start: dict[str, Any],
    _android_real_device: MagicMock,
) -> None:
    """Pack-backed start payload must carry BOTH legacy routing caps
    (appium:gridfleet:deviceId, appium:platform, appium:device_type)
    AND manifest caps (platformName, appium:automationName) simultaneously.
    Fails if pack_overrides replaces instead of merges.
    """
    await seed_test_packs(db_session)
    await db_session.commit()

    # Stub management host lookup and legacy stereotype so the assertions are deterministic.
    monkeypatch.setattr(node_service, "require_management_host", lambda device, action: _FakeHost())

    legacy_caps = {
        "appium:gridfleet:deviceId": str(_android_real_device.id),
        "appium:gridfleet:deviceName": _android_real_device.name,
        "appium:platform": "android_mobile",
        "appium:device_type": "real_device",
        "appium:os_version": "14",
        "appium:manufacturer": "Google",
        "appium:model": "Pixel 6",
    }
    monkeypatch.setattr(
        node_service,
        "build_agent_start_payload",
        lambda device, port, **kwargs: {
            "connection_target": "ABCD1234",
            "platform_id": "android_mobile",
            "port": port,
            "grid_url": None,
            "plugins": None,
            "extra_caps": None,
            "stereotype_caps": legacy_caps,
            "device_type": "real_device",
            "ip_address": None,
            "allocated_caps": None,
            "session_override": None,
            "headless": True,
        },
    )

    await node_service.start_remote_temporary_node(
        db_session,
        _android_real_device,
        port=4723,
        allocated_caps=None,
        agent_base="http://starts.local:5100",
        http_client_factory=AsyncMock(),
    )

    payload = _patched_remote_start["payload"]
    assert payload["pack_id"] == "appium-uiautomator2"
    assert payload["platform_id"] == "android_mobile"
    assert payload["insecure_features"] == ["uiautomator2:chromedriver_autodownload"]

    stereotype = payload["stereotype_caps"]
    # Legacy routing caps must survive the merge.
    assert stereotype["appium:gridfleet:deviceId"] == str(_android_real_device.id)
    assert stereotype["appium:platform"] == "android_mobile"
    assert stereotype["appium:device_type"] == "real_device"
    assert stereotype["appium:os_version"] == "14"
    # Manifest caps must be present.
    assert stereotype["platformName"] == "Android"
    assert stereotype["appium:automationName"] == "UiAutomator2"


@pytest.mark.asyncio
async def test_temporary_start_forwards_pack_workaround_env(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    _patched_remote_start: dict[str, Any],
) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    device = Device(
        id=uuid.uuid4(),
        pack_id="appium-xcuitest",
        platform_id="tvos",
        identity_scheme="apple_udid",
        identity_scope="global",
        identity_value="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        connection_target="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
        name="Living Room",
        os_version="26.4",
        host_id=uuid.uuid4(),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        ip_address="192.168.1.5",
        device_config={"wda_base_url": "http://192.168.1.5"},
    )

    monkeypatch.setattr(node_service, "require_management_host", lambda device, action: _FakeHost())
    monkeypatch.setattr(
        node_service,
        "build_agent_start_payload",
        lambda device, port, **kwargs: {
            "connection_target": device.connection_target,
            "platform_id": device.platform_id,
            "port": port,
            "grid_url": None,
            "plugins": None,
            "extra_caps": None,
            "stereotype_caps": {},
            "device_type": device.device_type.value,
            "ip_address": device.ip_address,
            "allocated_caps": None,
            "session_override": None,
            "headless": True,
        },
    )

    await node_service.start_remote_temporary_node(
        db_session,
        device,
        port=4723,
        allocated_caps=None,
        agent_base="http://starts.local:5100",
        http_client_factory=AsyncMock(),
    )

    assert _patched_remote_start["payload"]["workaround_env"] == {"APPIUM_XCUITEST_PREFER_DEVICECTL": "1"}


@pytest.mark.asyncio
async def test_temporary_start_sends_device_field_caps_only_to_appium_defaults(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_test_packs(db_session)
    await db_session.commit()

    device = Device(
        id=uuid.uuid4(),
        pack_id="appium-roku-dlenroc",
        platform_id="roku_network",
        identity_scheme="roku_serial",
        identity_scope="global",
        identity_value="roku-serial",
        connection_target="192.168.1.2",
        name="Roku Stick",
        os_version="15.1.4",
        ip_address="192.168.1.2",
        host_id=uuid.uuid4(),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        device_config={"roku_password": "dev-password"},
    )

    captured: dict[str, Any] = {}

    async def _fake_appium_start(
        agent_base: str, *, host: str, payload: dict[str, Any], **kwargs: Any
    ) -> _FakeHttpxResponse:
        captured["payload"] = payload
        return _FakeHttpxResponse(
            {
                "port": payload["port"],
                "pid": 3,
                "connection_target": payload["connection_target"],
            }
        )

    async def _noop_ready(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(node_service, "require_management_host", lambda device, action: _FakeHost())
    monkeypatch.setattr(node_service, "appium_start", _fake_appium_start)
    monkeypatch.setattr(node_service, "_wait_for_remote_appium_ready", _noop_ready)

    await node_service.start_remote_temporary_node(
        db_session,
        device,
        port=4724,
        allocated_caps=None,
        agent_base="http://starts.local:5100",
        http_client_factory=AsyncMock(),
    )

    payload = captured["payload"]
    assert payload["extra_caps"]["appium:password"] == "dev-password"
    assert payload["extra_caps"]["appium:ip"] == "192.168.1.2"
    assert "appium:password" not in payload["stereotype_caps"]


@pytest.mark.asyncio
async def test_restart_merges_pack_stereotype_over_legacy_caps(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parallel assertion for the restart path (`restart_node_via_agent` at
    `backend/app/services/node_service.py:288`, containing the
    `restart_pack_overrides` block around line 313-317).
    """
    await seed_test_packs(db_session)

    # Persist a real host so the Device FK constraint is satisfied.
    host = Host(
        hostname=f"restart-test-host-{uuid.uuid4().hex[:8]}",
        ip="10.0.2.1",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()

    # Use a real Device ORM instance so lock_device's SELECT FOR UPDATE finds the row.
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=f"restart-test-{uuid.uuid4().hex[:8]}",
        connection_target="ABCD1234",
        name="gate-pixel",
        model="Pixel 6",
        manufacturer="Google",
        os_version="14",
        host_id=host.id,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        ip_address=None,
        tags={},
    )
    db_session.add(device)
    await db_session.flush()

    # Persist a matching AppiumNode so lock_appium_node_for_device finds the row.
    appium_node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://localhost:4444",
        state=NodeState.running,
    )
    db_session.add(appium_node)
    await db_session.commit()

    captured: dict[str, Any] = {}

    async def _fake_appium_start(
        agent_base: str, *, host: str, payload: dict[str, Any], **kwargs: Any
    ) -> _FakeHttpxResponse:
        captured["payload"] = payload
        return _FakeHttpxResponse(
            {
                "port": payload["port"],
                "pid": 2,
                "connection_target": payload["connection_target"],
            }
        )

    async def _fake_appium_stop(agent_base: str, *, host: str, port: int, **kwargs: Any) -> _FakeHttpxResponse:
        return _FakeHttpxResponse({"stopped": True})

    async def _noop_ready(*args: Any, **kwargs: Any) -> None:
        return None

    async def _noop_session_aligned(*args: Any, **kwargs: Any) -> None:
        return None

    async def _noop_sleep(*args: Any, **kwargs: Any) -> None:
        return None

    async def _noop_get_owner_capabilities(*args: Any, **kwargs: Any) -> None:
        return None

    legacy_caps = {
        "appium:gridfleet:deviceId": str(device.id),
        "appium:platform": "android_mobile",
        "appium:device_type": "real_device",
    }

    monkeypatch.setattr(node_service, "require_management_host", lambda device, action: _FakeHost())
    monkeypatch.setattr(node_service, "appium_start", _fake_appium_start)
    monkeypatch.setattr(node_service, "appium_stop", _fake_appium_stop)
    monkeypatch.setattr(node_service, "_wait_for_remote_appium_ready", _noop_ready)
    monkeypatch.setattr(node_service, "_build_session_aligned_start_caps", _noop_session_aligned)
    monkeypatch.setattr(node_service.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(
        node_service.appium_resource_allocator,
        "get_owner_capabilities",
        _noop_get_owner_capabilities,
    )
    monkeypatch.setattr(
        node_service,
        "build_agent_start_payload",
        lambda device, port, **kwargs: {
            "connection_target": "ABCD1234",
            "platform_id": "android_mobile",
            "port": port,
            "grid_url": None,
            "plugins": None,
            "extra_caps": None,
            "stereotype_caps": dict(legacy_caps),
            "device_type": "real_device",
            "ip_address": None,
            "allocated_caps": None,
            "session_override": None,
            "headless": True,
        },
    )

    from app.services.node_service import restart_node_via_agent

    node = MagicMock()
    node.port = 4723

    await restart_node_via_agent(
        db_session,
        device,
        node,
        http_client_factory=AsyncMock(),
    )

    payload = captured["payload"]
    assert payload["pack_id"] == "appium-uiautomator2"
    assert payload["platform_id"] == "android_mobile"
    assert payload["insecure_features"] == ["uiautomator2:chromedriver_autodownload"]
    stereotype = payload["stereotype_caps"]
    assert stereotype["appium:gridfleet:deviceId"] == str(device.id)
    assert stereotype["appium:platform"] == "android_mobile"
    assert stereotype["appium:device_type"] == "real_device"
    assert stereotype["platformName"] == "Android"
    assert stereotype["appium:automationName"] == "UiAutomator2"


@pytest.mark.asyncio
async def test_start_payload_sends_manifest_appium_platform_name(db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="SERIAL1",
        connection_target="SERIAL1",
        name="Pixel",
        os_version="14",
        host_id=uuid.uuid4(),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )

    payload = build_agent_start_payload(device, 4723)
    stereotype = await render_stereotype(
        db_session,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
    )
    pack_payload = await build_pack_start_payload(db_session, device=device, stereotype=stereotype)

    assert pack_payload is not None
    payload.update(
        {
            "pack_id": pack_payload["pack_id"],
            "platform_id": pack_payload["platform_id"],
            "appium_platform_name": pack_payload["appium_platform_name"],
        }
    )

    assert payload["appium_platform_name"] == "Android"
    assert payload["platform_id"] == "android_mobile"
    assert "platform_name" not in payload


@pytest.mark.asyncio
async def test_pack_owned_device_missing_catalog_raises(db_session: AsyncSession) -> None:
    device = Device(
        pack_id="missing-pack",
        platform_id="missing-platform",
        identity_scheme="vendor_serial",
        identity_scope="host",
        identity_value="SERIAL1",
        connection_target="SERIAL1",
        name="Missing Catalog Device",
        os_version="1",
        host_id=uuid.uuid4(),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )

    with pytest.raises(PackStartPayloadError, match="missing-pack:missing-platform"):
        await build_pack_start_payload(db_session, device=device)
