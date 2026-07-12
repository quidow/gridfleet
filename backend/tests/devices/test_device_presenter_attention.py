import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import ConnectionType, Device, DeviceIntent, DeviceOperationalState, DeviceType, ExclusionKind
from app.devices.services import presenter as device_presenter
from app.devices.services.presenter import DevicePresenterService
from tests.fakes import FakeSettingsReader

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


async def test_serialize_device_includes_needs_attention(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="ATTN-DEV-1",
        connection_target="ATTN-DEV-1",
        name="Attention Test",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.offline,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()
    await db_session.refresh(device)

    payload = await DevicePresenterService(settings=FakeSettingsReader({})).serialize_device(db_session, device)
    assert payload["needs_attention"] is True


async def test_serialize_device_review_required_needs_attention(db_session: AsyncSession, db_host: Host) -> None:
    # S10 finding: a device shelved pending operator review IS a device needing
    # attention, even when it is otherwise healthy/verified/available.
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="firetv_real",
        identity_scheme="android_serial",
        identity_scope="global",
        identity_value="G070VM9999999999",
        connection_target="192.168.1.50:5555",
        name="Review Shelved",
        os_version="6.0",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        review_required=True,
    )
    db_session.add(device)
    await db_session.commit()
    await db_session.refresh(device)

    payload = await DevicePresenterService(settings=FakeSettingsReader({})).serialize_device(db_session, device)
    assert payload["needs_attention"] is True


async def test_serialize_device_includes_extended_device_info(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="firetv_real",
        identity_scheme="android_serial",
        identity_scope="global",
        identity_value="G070VM1234567890",
        connection_target="192.168.1.99:5555",
        name="Fire TV Stick 4K",
        os_version="6.0",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        manufacturer="Amazon",
        model="Fire TV Stick 4K",
        model_number="AFTMM",
        software_versions={"fire_os": "6.0", "android": "7.1.2", "build": "NS6271/2495"},
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        ip_address="192.168.1.99",
    )
    db_session.add(device)
    await db_session.commit()
    await db_session.refresh(device)

    payload = await DevicePresenterService(settings=FakeSettingsReader({})).serialize_device(db_session, device)

    assert payload["model"] == "Fire TV Stick 4K"
    assert payload["model_number"] == "AFTMM"
    assert payload["software_versions"] == {
        "fire_os": "6.0",
        "android": "7.1.2",
        "build": "NS6271/2495",
    }


def test_build_reservation_read_marks_escalated_cooldown() -> None:
    reservation = SimpleNamespace(id=uuid.uuid4(), name="run", state=SimpleNamespace(value="active"))
    entry = SimpleNamespace(
        excluded=True,
        exclusion_kind=ExclusionKind.cooldown,
        exclusion_reason="Exceeded cooldown threshold 3",
        excluded_until=datetime.now(UTC) + timedelta(seconds=30),
        cooldown_count=4,
    )

    payload = device_presenter.build_reservation_read(reservation, entry)

    assert payload is not None
    assert payload.cooldown_escalated is True
    assert payload.cooldown_remaining_sec is not None


async def test_serialize_orchestration_includes_intent_kinds() -> None:
    intents = [
        DeviceIntent(device_id=uuid.uuid4(), source="node", kind="operator:start", payload={"action": "start"}),
        DeviceIntent(device_id=uuid.uuid4(), source="stop", kind="operator:stop:node", payload={"action": "stop"}),
        DeviceIntent(
            device_id=uuid.uuid4(), source="recovery", kind="operator:stop:recovery", payload={"allowed": False}
        ),
    ]

    class Result:
        def __init__(self, rows: list[DeviceIntent]) -> None:
            self._rows = rows

        def scalars(self) -> Result:
            return self

        def all(self) -> list[DeviceIntent]:
            return self._rows

        def scalar_one_or_none(self) -> None:  # the reservation-facts query
            return None

    class Session:
        def __init__(self) -> None:
            self._execute_count = 0

        async def execute(self, *_args: object, **_kwargs: object) -> Result:
            self._execute_count += 1
            # The first query loads intents; later queries are the reservation
            # and remediation-log fact reads used by gather_decision_facts.
            return Result(intents if self._execute_count == 1 else [])

    device = SimpleNamespace(
        id=uuid.uuid4(),
        lifecycle_policy_state={},
        device_checks_healthy=None,
        verified_at=None,
        review_required=False,
    )
    payload = await device_presenter._serialize_orchestration(Session(), device)  # type: ignore[arg-type]

    assert [item["kind"] for item in payload["intents"]] == [
        "operator:start",
        "operator:stop:node",
        "operator:stop:recovery",
    ]
    assert "node_process" in payload["derived"]
    assert "grid_routing" in payload["derived"]
    assert "recovery" in payload["derived"]


async def test_serialize_device_detail_adds_node_and_orchestration(monkeypatch: pytest.MonkeyPatch) -> None:
    _node = AppiumNode(
        id=uuid.uuid4(),
        device_id=uuid.uuid4(),
        port=4723,
        desired_state=AppiumDesiredState.running,
    )
    device = SimpleNamespace(
        id=uuid.uuid4(),
        appium_node=_node,
        lifecycle_policy_state={"last_action": "recovery_started"},
        review_required=False,
    )
    svc = DevicePresenterService(settings=FakeSettingsReader({}))
    monkeypatch.setattr(svc, "serialize_device", AsyncMock(return_value={"id": "device"}))
    monkeypatch.setattr(device_presenter, "_serialize_orchestration", AsyncMock(return_value={"intents": []}))
    monkeypatch.setattr(
        device_presenter.remediation_log,
        "load_ladder",
        AsyncMock(return_value=device_presenter.remediation_log.EMPTY_LADDER),
    )

    payload = await svc.serialize_device_detail(AsyncMock(), device, include_orchestration=True)

    assert payload["appium_node"]["port"] == 4723
    assert payload["orchestration"] == {"intents": []}
