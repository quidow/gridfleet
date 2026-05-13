import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.models.device_intent import DeviceIntent
from app.models.host import Host
from app.services import device_presenter


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

    payload = await device_presenter.serialize_device(db_session, device)
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

    payload = await device_presenter.serialize_device(db_session, device)

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
        exclusion_reason="Exceeded cooldown threshold 3",
        excluded_until=datetime.now(UTC) + timedelta(seconds=30),
        cooldown_count=4,
    )

    payload = device_presenter.build_reservation_read(reservation, entry)

    assert payload is not None
    assert payload.cooldown_escalated is True
    assert payload.cooldown_remaining_sec is not None


async def test_serialize_orchestration_splits_intent_axes() -> None:
    intents = [
        DeviceIntent(device_id=uuid.uuid4(), source="node", axis="node_process", payload={"action": "start"}),
        DeviceIntent(device_id=uuid.uuid4(), source="grid", axis="grid_routing", payload={"enabled": True}),
        DeviceIntent(device_id=uuid.uuid4(), source="reservation", axis="reservation", payload={"hold": True}),
        DeviceIntent(device_id=uuid.uuid4(), source="recovery", axis="recovery", payload={"allowed": True}),
    ]

    class Result:
        def scalars(self) -> "Result":
            return self

        def all(self) -> list[DeviceIntent]:
            return intents

    class Session:
        async def execute(self, *_args: object, **_kwargs: object) -> Result:
            return Result()

    payload = await device_presenter._serialize_orchestration(Session(), SimpleNamespace(id=uuid.uuid4()))  # type: ignore[arg-type]

    assert [item["axis"] for item in payload["intents"]] == [
        "node_process",
        "grid_routing",
        "reservation",
        "recovery",
    ]
    assert "node_process" in payload["derived"]
    assert "reservation" in payload["derived"]


async def test_serialize_device_detail_adds_node_sessions_and_orchestration(monkeypatch) -> None:  # noqa: ANN001
    device = SimpleNamespace(
        appium_node=AppiumNode(
            id=uuid.uuid4(),
            device_id=uuid.uuid4(),
            port=4723,
            grid_url="http://grid",
            desired_state=AppiumDesiredState.running,
        ),
        lifecycle_policy_state={"last_action": "recovery_started"},
        sessions=["session"],
    )
    monkeypatch.setattr(device_presenter, "serialize_device", AsyncMock(return_value={"id": "device"}))
    monkeypatch.setattr(device_presenter, "_serialize_orchestration", AsyncMock(return_value={"intents": []}))

    payload = await device_presenter.serialize_device_detail(AsyncMock(), device)

    assert payload["appium_node"]["port"] == 4723
    assert payload["sessions"] == ["session"]
    assert payload["orchestration"] == {"intents": []}
