from datetime import timedelta

from app.models.host import HostStatus, OSType
from app.seeding.context import SeedContext
from app.seeding.factories.host import make_host


def test_make_host_applies_required_fields() -> None:
    ctx = SeedContext.build(session=None, seed=42)  # type: ignore[arg-type]
    host = make_host(
        ctx,
        hostname="lab-linux-01",
        ip="10.0.0.11",
        os_type=OSType.linux,
        status=HostStatus.online,
        last_heartbeat_offset=timedelta(seconds=-5),
        agent_version="1.5.0",
    )
    assert host.hostname == "lab-linux-01"
    assert host.os_type is OSType.linux
    assert host.status is HostStatus.online
    assert host.last_heartbeat is not None
    assert host.last_heartbeat == ctx.now + timedelta(seconds=-5)
    assert host.agent_version == "1.5.0"


def test_make_host_offline_host_has_stale_heartbeat() -> None:
    ctx = SeedContext.build(session=None, seed=1)  # type: ignore[arg-type]
    host = make_host(
        ctx,
        hostname="lab-linux-02",
        ip="10.0.0.12",
        os_type=OSType.linux,
        status=HostStatus.offline,
        last_heartbeat_offset=timedelta(minutes=-23),
    )
    assert host.last_heartbeat == ctx.now + timedelta(minutes=-23)
