import uuid
from datetime import timedelta

from app.models.device_event import DeviceEventType
from app.seeding.context import SeedContext
from app.seeding.factories.event import make_device_event, make_system_event


def test_make_device_event_uses_timestamp_offset() -> None:
    ctx = SeedContext.build(session=None, seed=1)  # type: ignore[arg-type]
    device_id = uuid.uuid4()
    evt = make_device_event(
        ctx,
        device_id=device_id,
        event_type=DeviceEventType.connectivity_lost,
        created_at=ctx.now - timedelta(minutes=5),
        details={"reason": "adb usb unplug"},
    )
    assert evt.device_id == device_id
    assert evt.event_type is DeviceEventType.connectivity_lost
    assert evt.details == {"reason": "adb usb unplug"}
    assert evt.created_at == ctx.now - timedelta(minutes=5)


def test_make_system_event_generates_unique_event_id() -> None:
    ctx = SeedContext.build(session=None, seed=1)  # type: ignore[arg-type]
    a = make_system_event(ctx, event_type="run.completed", data={"run_id": "1"}, created_at=ctx.now)
    b = make_system_event(ctx, event_type="run.completed", data={"run_id": "2"}, created_at=ctx.now)
    assert a.event_id != b.event_id
    assert a.type == "run.completed"
