from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services import state_write_guard
from app.hosts.models import Host
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.runs.models import RunState, TestRun
from app.runs.service_reservation import RunReservationService
from app.sessions.models import Session, SessionStatus
from app.sessions.protocols import SessionSyncProtocol
from app.sessions.service_sync import SessionSyncService
from tests.fakes import FakeSettingsReader, make_fake_grid
from tests.helpers import test_event_bus as event_bus

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")

_GRID_UP_EMPTY: dict[str, Any] = {"value": {"ready": True, "nodes": []}}


def _make_real_lifecycle(publisher: object = None) -> LifecyclePolicyService:
    """Return a real LifecyclePolicyService for tests that need actual DB mutations."""
    pub = publisher if publisher is not None else event_bus
    return LifecyclePolicyService(
        publisher=pub,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=pub, reservation=RunReservationService(), incidents=LifecycleIncidentService()
        ),
        incidents=LifecycleIncidentService(),
        viability=Mock(),
        node_manager=AsyncMock(),
    )


def _make_sync_service(grid_data: dict[str, Any] | None = None) -> SessionSyncService:
    return SessionSyncService(
        publisher=AsyncMock(),
        settings=FakeSettingsReader({}),
        grid=make_fake_grid(grid_data if grid_data is not None else _GRID_UP_EMPTY),
        lifecycle=AsyncMock(),
    )


async def _sync_sessions(db: AsyncSession) -> None:
    await _make_sync_service().sync(db)


# Backward-compat alias used by test bodies that call _sync_sessions_impl directly
async def _sync_sessions_impl(
    db: AsyncSession,
    *,
    settings: object = None,
    publisher: object = None,
    grid: object = None,
    lifecycle: object = None,
) -> None:
    svc = SessionSyncService(
        publisher=publisher if publisher is not None else AsyncMock(),
        settings=settings if settings is not None else FakeSettingsReader({}),
        grid=grid if grid is not None else make_fake_grid(_GRID_UP_EMPTY),
        lifecycle=lifecycle if lifecycle is not None else AsyncMock(),
    )
    await svc.sync(db)


@pytest.fixture(autouse=True)
def _skip_leader_fencing() -> Iterator[None]:
    """No-op assert_current_leader so unit tests don't need a real leader row."""
    with patch("app.sessions.service_sync.assert_current_leader"):
        yield


def _grid_response(sessions_per_node: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build a mock Grid /status response.

    Grid 4 stores sessions under node.slots[].session (not node.sessions).
    """
    if sessions_per_node is None:
        sessions_per_node = []
    slots: list[dict[str, Any]] = []
    for sess in sessions_per_node:
        slots.append({"session": sess})
    # Pad with empty slots so the node has slots even with no active sessions
    if not slots:
        slots.append({"session": None})
    return {
        "value": {
            "ready": True,
            "message": "Selenium Grid ready.",
            "nodes": [
                {
                    "id": "node-1",
                    "slots": slots,
                    "availability": "UP",
                }
            ],
        }
    }


def _grid_session(
    session_id: str,
    connection_target: str,
    test_name: str | None = None,
    device_id: str | None = None,
    *,
    is_probe: bool = False,
) -> dict[str, Any]:
    """Build a single slot session entry mirroring the real Selenium 4 hub shape.

    The Appium driver strips the ``appium:`` prefix from the matched W3C
    capabilities it returns at session start, so ``slot.session.capabilities``
    in production exposes bare ``udid`` / ``deviceName`` and never echoes
    ``appium:gridfleet:deviceId`` back. Identity therefore has to be read
    from ``slot.session.stereotype`` (the agent-advertised block, which
    keeps its prefixes verbatim). Probe / test_name markers live in the
    ``gridfleet:`` vendor namespace which the driver does pass through
    on ``capabilities``.
    """
    caps: dict[str, Any] = {
        "platformName": "android",
        "udid": connection_target,
        "deviceName": connection_target,
    }
    if test_name:
        caps["gridfleet:testName"] = test_name
    if is_probe:
        caps["gridfleet:probeSession"] = True

    stereotype: dict[str, Any] = {
        "platformName": "android",
        "appium:udid": connection_target,
    }
    if device_id:
        stereotype["appium:gridfleet:deviceId"] = device_id

    return {"sessionId": session_id, "capabilities": caps, "stereotype": stereotype}


async def test_sync_tracks_real_hub_payload_with_stripped_capabilities(db_session: AsyncSession, db_host: Host) -> None:
    """Pin the long-term fix for the silent-skip bug.

    Selenium 4.41 + Appium UiAutomator2 returns ``slot.session.capabilities``
    with the ``appium:`` prefix stripped and ``appium:gridfleet:deviceId``
    omitted entirely (the driver does not echo unknown vendor keys back into
    the W3C capabilities response). Identity must be resolved from
    ``slot.session.stereotype`` instead — that block carries the
    agent-advertised, prefix-stable caps verbatim.

    The literal payload below is copied from a live hub probe so the test
    fails the moment a regression starts reading identity from
    ``capabilities`` again.
    """
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="firetv_real",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="192.168.1.254:5555",
            connection_target="192.168.1.254:5555",
            name="Fire TV Stick 4K",
            os_version="6",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.network,
        )
    db_session.add(device)
    await db_session.commit()

    grid_data = {
        "value": {
            "ready": True,
            "nodes": [
                {
                    "id": "node-real",
                    "availability": "UP",
                    "slots": [
                        {
                            "session": {
                                "sessionId": "017b37b0-c2a8-4f90-8c3a-df4f7685944b",
                                "start": "2026-05-16T14:12:03.733064Z",
                                "uri": "http://192.168.88.92:5558",
                                "capabilities": {
                                    "automationName": "UiAutomator2",
                                    "platformName": "ANDROID",
                                    "udid": "192.168.1.254:5555",
                                    "deviceName": "192.168.1.254:5555",
                                },
                                "stereotype": {
                                    "appium:automationName": "UiAutomator2",
                                    "appium:device_type": "real_device",
                                    "appium:gridfleet:deviceId": str(device.id),
                                    "appium:udid": "192.168.1.254:5555",
                                    "platformName": "ANDROID",
                                },
                            },
                        }
                    ],
                }
            ],
        }
    }

    await _sync_sessions_impl(
        db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(grid_data)
    )

    result = await db_session.execute(
        select(Session).where(Session.session_id == "017b37b0-c2a8-4f90-8c3a-df4f7685944b")
    )
    session = result.scalar_one()
    assert session.status == SessionStatus.running
    assert session.device_id == device.id

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.busy


async def test_sync_hydrates_orphan_session_row_from_hub_stereotype(db_session: AsyncSession, db_host: Host) -> None:
    """Real-world testkit flow: client registers a session with no
    ``device_id`` / ``connection_target`` (the Appium echo strips the
    identifying caps), so the row lands with ``device_id IS NULL`` and the
    device stays ``available``. The sync loop must read the hub's
    ``slot.session.stereotype`` (which carries ``appium:gridfleet:deviceId``
    verbatim), bind the row to its device, and fire the busy transition.
    """
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="hydrate-target",
            connection_target="hydrate-target",
            name="Hydrate Phone",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()

    # Orphan row created earlier by ``POST /api/sessions`` from the testkit.
    orphan = Session(
        session_id="orphan-sess",
        device_id=None,
        test_name="test_orphan",
        status=SessionStatus.running,
        requested_capabilities={
            "udid": "hydrate-target",
            "deviceName": "hydrate-target",
            "automationName": "UiAutomator2",
        },
    )
    db_session.add(orphan)
    await db_session.commit()

    grid_data = _grid_response([_grid_session("orphan-sess", "hydrate-target", device_id=str(device.id))])
    await _sync_sessions_impl(
        db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(grid_data)
    )

    await db_session.refresh(orphan)
    assert orphan.device_id == device.id
    assert orphan.status == SessionStatus.running

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.busy


async def test_sync_hydrate_orphan_does_not_attach_run_id_when_preparing(
    db_session: AsyncSession, db_host: Host
) -> None:
    from tests.helpers import create_reserved_run

    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="hydrate-prep",
            connection_target="hydrate-prep",
            name="Hydrate Prep Phone",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    run = await create_reserved_run(db_session, name="Prep Hydration Run", devices=[device], state=RunState.preparing)

    orphan = Session(
        session_id="orphan-prep",
        device_id=None,
        test_name="prep-warmup",
        status=SessionStatus.running,
    )
    db_session.add(orphan)
    await db_session.commit()

    grid_data = _grid_response([_grid_session("orphan-prep", "hydrate-prep", device_id=str(device.id))])
    await _sync_sessions_impl(
        db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(grid_data)
    )

    await db_session.refresh(orphan)
    assert orphan.device_id == device.id
    assert orphan.run_id is None

    await db_session.refresh(run)
    assert run.state == RunState.preparing
    assert run.started_at is None


async def test_sync_hydrate_orphan_attaches_run_id_when_active(db_session: AsyncSession, db_host: Host) -> None:
    from tests.helpers import create_reserved_run

    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="hydrate-active",
            connection_target="hydrate-active",
            name="Hydrate Active Phone",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    run = await create_reserved_run(db_session, name="Active Hydration Run", devices=[device], state=RunState.active)

    orphan = Session(
        session_id="orphan-active",
        device_id=None,
        test_name="real-test",
        status=SessionStatus.running,
    )
    db_session.add(orphan)
    await db_session.commit()

    grid_data = _grid_response([_grid_session("orphan-active", "hydrate-active", device_id=str(device.id))])
    await _sync_sessions_impl(
        db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(grid_data)
    )

    await db_session.refresh(orphan)
    assert orphan.device_id == device.id
    assert orphan.run_id == run.id


async def test_sync_creates_session_does_not_attach_run_id_when_preparing(
    db_session: AsyncSession, db_host: Host
) -> None:
    from tests.helpers import create_reserved_run

    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="sync-prep",
            connection_target="sync-prep",
            name="Sync Prep Phone",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    run = await create_reserved_run(db_session, name="Sync Prep Run", devices=[device], state=RunState.preparing)

    grid_data = _grid_response([_grid_session("sync-prep-sid", "sync-prep", "prep-warmup", device_id=str(device.id))])
    await _sync_sessions_impl(
        db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(grid_data)
    )

    result = await db_session.execute(select(Session).where(Session.session_id == "sync-prep-sid"))
    session = result.scalar_one()
    assert session.run_id is None

    await db_session.refresh(run)
    assert run.state == RunState.preparing
    assert run.started_at is None


async def test_sync_creates_session(db_session: AsyncSession, db_host: Host) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="dev-001",
            connection_target="dev-001",
            name="Test Phone",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()

    grid_data = _grid_response([_grid_session("sess-1", "dev-001", "test_login")])

    await _sync_sessions_impl(
        db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(grid_data)
    )

    result = await db_session.execute(select(Session).where(Session.session_id == "sess-1"))
    session = result.scalar_one()
    assert session.status == SessionStatus.running
    assert session.test_name == "test_login"
    assert session.device_id == device.id

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.busy


async def test_sync_ends_session(db_session: AsyncSession, db_host: Host) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="dev-002",
            connection_target="dev-002",
            name="Test Phone",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()

    session = Session(session_id="sess-2", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    # Grid reports no sessions
    grid_data = _grid_response([])

    await _sync_sessions_impl(
        db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(grid_data)
    )

    await db_session.refresh(session)
    assert session.status == SessionStatus.passed
    assert session.ended_at is not None

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available


async def test_sync_ends_session_marks_offline_when_node_stop_pending(db_session: AsyncSession, db_host: Host) -> None:
    """When a session ends and the device's Appium node already has a
    graceful-stop intent registered, the device must transition
    busy → offline, not busy → available.

    The intent reconciler was held while the session was running (the
    session-safety invariant) and now applies, writing
    ``desired_state=stopped`` + ``stop_pending=True``. Without the
    ``appium_node_stop_in_flight`` gate in ``ready_operational_state``,
    the allocator could briefly pick the device before the agent finishes
    deregistering the relay from the hub.
    """
    from app.appium_nodes.models import AppiumDesiredState, AppiumNode
    from app.devices.services.intent import IntentService
    from app.devices.services.intent_types import NODE_PROCESS, PRIORITY_HEALTH_FAILURE, IntentRegistration

    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="dev-stop-pending",
            connection_target="dev-stop-pending",
            name="Stop Pending Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=42,
            active_connection_target=device.connection_target,
        )
    db_session.add(node)
    session = Session(session_id="sess-stop-pending", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    service = IntentService(db_session)
    await service.register_intents(
        device_id=device.id,
        reason="held graceful stop",
        intents=[
            IntentRegistration(
                source=f"health_failure:node:{device.id}",
                axis=NODE_PROCESS,
                payload={"action": "stop", "stop_mode": "graceful", "priority": PRIORITY_HEALTH_FAILURE},
            ),
        ],
    )
    await db_session.commit()

    grid_data = _grid_response([])

    await _sync_sessions_impl(
        db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(grid_data)
    )

    await db_session.refresh(session)
    assert session.ended_at is not None
    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline
    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped
    assert node.stop_pending is True


async def test_sync_ends_duplicate_running_sessions(db_session: AsyncSession, db_host: Host) -> None:
    """Two Session rows share the same session_id with status=running.

    Reproduces the production crash where session_sync.scalar_one_or_none()
    raised MultipleResultsFound, deadlocking the loop and leaving devices
    stuck busy. ``ux_sessions_session_id_running`` now blocks new duplicates,
    but rows that pre-date the migration can still exist; the loop must
    survive them and end every matching row.
    """
    from sqlalchemy import text

    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="dev-dup",
            connection_target="dev-dup",
            name="Duplicate Phone",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()

    # Drop the partial unique index just for this test so we can simulate the
    # pre-migration state where two ``running`` rows shared a ``session_id``.
    await db_session.execute(text("DROP INDEX ux_sessions_session_id_running"))
    try:
        dup_a = Session(session_id="sess-dup", device_id=device.id, status=SessionStatus.running)
        dup_b = Session(session_id="sess-dup", device_id=device.id, status=SessionStatus.running)
        db_session.add_all([dup_a, dup_b])
        await db_session.commit()

        grid_data = _grid_response([])

        await _sync_sessions_impl(
            db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(grid_data)
        )

        result = await db_session.execute(select(Session).where(Session.session_id == "sess-dup"))
        rows = result.scalars().all()
        assert len(rows) == 2
        for row in rows:
            assert row.status == SessionStatus.passed
            assert row.ended_at is not None

        await db_session.refresh(device)
        assert device.operational_state == DeviceOperationalState.available
    finally:
        # Force any lingering duplicate ``running`` rows to a terminal state
        # before recreating the partial unique index. Without this, a failure
        # in the test body (e.g. the loop tolerance regression returns) would
        # leave duplicates in place and the CREATE INDEX below would raise
        # IntegrityError, masking the original assertion error.
        await db_session.rollback()
        await db_session.execute(
            text(
                "UPDATE sessions SET status = 'error', ended_at = NOW() "
                "WHERE session_id = 'sess-dup' AND status = 'running' AND ended_at IS NULL"
            )
        )
        await db_session.execute(
            text(
                "CREATE UNIQUE INDEX ux_sessions_session_id_running ON sessions (session_id) "
                "WHERE status = 'running' AND ended_at IS NULL"
            )
        )
        await db_session.commit()


async def test_sync_ends_duplicate_running_sessions_across_devices(db_session: AsyncSession, db_host: Host) -> None:
    """Two ``running`` rows share a ``session_id`` but reference different devices.

    Even though ``ux_sessions_session_id_running`` blocks this in fresh
    installs, legacy data (pre-migration races, agent reassignments) can
    leave a single ``session_id`` mapped to multiple device rows. The
    ended-session sweep must move ``operational_state`` off busy on every
    affected device, not only on the one that ``known_running`` happened
    to retain after dict overwrite.
    """
    from sqlalchemy import text

    with state_write_guard.bypass():
        device_a = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="dev-dup-multi-a",
            connection_target="dev-dup-multi-a",
            name="Duplicate Phone A",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    with state_write_guard.bypass():
        device_b = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="dev-dup-multi-b",
            connection_target="dev-dup-multi-b",
            name="Duplicate Phone B",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add_all([device_a, device_b])
    await db_session.flush()

    await db_session.execute(text("DROP INDEX ux_sessions_session_id_running"))
    try:
        dup_a = Session(session_id="sess-dup-multi", device_id=device_a.id, status=SessionStatus.running)
        dup_b = Session(session_id="sess-dup-multi", device_id=device_b.id, status=SessionStatus.running)
        db_session.add_all([dup_a, dup_b])
        await db_session.commit()

        await _sync_sessions_impl(
            db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(_grid_response([]))
        )

        rows = (await db_session.execute(select(Session).where(Session.session_id == "sess-dup-multi"))).scalars().all()
        assert len(rows) == 2
        assert all(row.ended_at is not None for row in rows)

        await db_session.refresh(device_a)
        await db_session.refresh(device_b)
        assert device_a.operational_state != DeviceOperationalState.busy, (
            "every device referenced by a duplicate ended session must move off busy"
        )
        assert device_b.operational_state != DeviceOperationalState.busy
    finally:
        await db_session.rollback()
        await db_session.execute(
            text(
                "UPDATE sessions SET status = 'error', ended_at = NOW() "
                "WHERE session_id = 'sess-dup-multi' AND status = 'running' AND ended_at IS NULL"
            )
        )
        await db_session.execute(
            text(
                "CREATE UNIQUE INDEX ux_sessions_session_id_running ON sessions (session_id) "
                "WHERE status = 'running' AND ended_at IS NULL"
            )
        )
        await db_session.commit()


async def test_sync_ends_session_after_identity_map_reset(db_session: AsyncSession, db_host: Host) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="dev-002b",
            connection_target="dev-002b",
            name="Reset Phone",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()

    session = Session(session_id="sess-2b", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()
    db_session.expunge_all()

    await _sync_sessions_impl(
        db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(_grid_response([]))
    )

    result = await db_session.execute(select(Session).where(Session.session_id == "sess-2b"))
    ended_session = result.scalar_one()
    assert ended_session.status == SessionStatus.passed
    assert ended_session.ended_at is not None

    refreshed_device = await db_session.get(Device, device.id)
    assert refreshed_device is not None
    assert refreshed_device.operational_state == DeviceOperationalState.available


async def test_sync_marks_late_ended_session_for_cancelled_run_as_error(
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.helpers import create_device_record, create_reserved_run

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="late-ended-cancel",
        connection_target="late-ended-cancel",
        name="Late Ended Cancel",
        operational_state=DeviceOperationalState.busy,
    )
    run = await create_reserved_run(
        db_session,
        name="Late Ended Cancel Run",
        devices=[device],
        state=RunState.cancelled,
        mark_released=True,
    )
    session = Session(
        session_id="late-ended-session",
        device_id=device.id,
        run_id=run.id,
        test_name="test_late_ended",
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    await _sync_sessions_impl(
        db_session,
        settings=FakeSettingsReader({}),
        publisher=AsyncMock(),
        grid=make_fake_grid({"value": {"ready": True, "nodes": []}}),
    )

    await db_session.refresh(session)
    assert session.status == SessionStatus.error
    assert session.error_type == "run_released"
    assert session.error_message == "Run ended while session was still running (cancelled)"
    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available


async def test_sync_ignores_unknown_connection_target(db_session: AsyncSession) -> None:
    grid_data = _grid_response([_grid_session("sess-3", "unknown-device")])

    await _sync_sessions_impl(
        db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(grid_data)
    )

    result = await db_session.execute(select(Session))
    assert result.scalars().all() == []


async def test_sync_uses_manager_device_id_when_udid_is_transient(db_session: AsyncSession, db_host: Host) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="manager_generated",
            identity_scope="host",
            identity_value="avd:Pixel_6_API_35",
            connection_target="Pixel_6_API_35",
            name="Pixel 6",
            os_version="15",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()

    grid_data = _grid_response([_grid_session("sess-avd", "emulator-5554", "test_login", device_id=str(device.id))])

    await _sync_sessions_impl(
        db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(grid_data)
    )

    result = await db_session.execute(select(Session).where(Session.session_id == "sess-avd"))
    session = result.scalar_one()
    assert session.device_id == device.id

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.busy


async def test_sync_preserves_busy_for_multi_session(db_session: AsyncSession, db_host: Host) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="dev-004",
            connection_target="dev-004",
            name="Multi Phone",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()

    s1 = Session(session_id="sess-4a", device_id=device.id, status=SessionStatus.running)
    s2 = Session(session_id="sess-4b", device_id=device.id, status=SessionStatus.running)
    db_session.add_all([s1, s2])
    await db_session.commit()

    # Only sess-4b is still running on Grid
    grid_data = _grid_response([_grid_session("sess-4b", "dev-004")])

    await _sync_sessions_impl(
        db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(grid_data)
    )

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.busy  # sess-4b still running


async def test_sync_startup_recovery(db_session: AsyncSession, db_host: Host) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="dev-005",
            connection_target="dev-005",
            name="Recovery Phone",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()

    session = Session(session_id="sess-5", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    # Grid still has the session running
    grid_data = _grid_response([_grid_session("sess-5", "dev-005")])

    await _sync_sessions_impl(
        db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(grid_data)
    )

    result = await db_session.execute(select(Session).where(Session.session_id == "sess-5"))
    sessions = result.scalars().all()
    assert len(sessions) == 1  # not duplicated


async def test_sync_does_not_duplicate_terminal_session_seen_active_again(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="dev-terminal-race",
            connection_target="dev-terminal-race",
            name="Terminal Race Phone",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()

    session = Session(
        session_id="sess-terminal-race",
        device_id=device.id,
        test_name="test_terminal_race",
        status=SessionStatus.passed,
        ended_at=datetime.now(UTC),
    )
    db_session.add(session)
    await db_session.commit()

    grid_data = _grid_response([_grid_session("sess-terminal-race", "dev-terminal-race", "test_terminal_race")])

    await _sync_sessions_impl(
        db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(grid_data)
    )

    result = await db_session.execute(select(Session).where(Session.session_id == "sess-terminal-race"))
    sessions = result.scalars().all()
    assert len(sessions) == 1
    assert sessions[0].status == SessionStatus.passed


async def test_sync_preserves_reserved_hold_after_session_end_for_reserved_run(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="dev-007",
            connection_target="dev-007",
            name="Reserved Return",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()

    run = TestRun(
        name="Reserved Return Run",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
                "excluded": False,
                "exclusion_reason": None,
                "excluded_at": None,
            }
        ],
    )
    session = Session(session_id="sess-7", device_id=device.id, status=SessionStatus.running)
    db_session.add_all([run, session])
    await db_session.commit()

    await _sync_sessions_impl(
        db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(_grid_response([]))
    )

    await db_session.refresh(device)
    assert device.operational_state != DeviceOperationalState.maintenance


async def test_sync_stops_deferred_unhealthy_device_after_session_end(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="dev-008",
            connection_target="dev-008",
            name="Deferred Stop",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()

    run = TestRun(
        name="Deferred Stop Run",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
                "excluded": False,
                "exclusion_reason": None,
                "excluded_at": None,
            }
        ],
    )
    session = Session(session_id="sess-8", device_id=device.id, status=SessionStatus.running)
    db_session.add_all([run, session])
    await db_session.commit()

    await _make_real_lifecycle(publisher=event_bus).handle_health_failure(
        db_session, device, source="device_checks", reason="ADB not responsive"
    )

    await _sync_sessions_impl(
        db_session,
        settings=FakeSettingsReader({}),
        publisher=AsyncMock(),
        grid=make_fake_grid(_grid_response([])),
        lifecycle=_make_real_lifecycle(),
    )

    await db_session.refresh(device)
    await db_session.refresh(run, ["device_reservations"])
    assert device.operational_state == DeviceOperationalState.offline
    assert run.reserved_devices is not None
    assert run.reserved_devices[0]["excluded"] is True


async def test_sync_restores_busy_when_deferred_stop_dropped_for_healthy_device(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """When `handle_session_finished` drops a deferred-stop intent because the
    device is currently healthy (defense-in-depth branch), it returns False so
    `_on_session_end` falls through to `ready_operational_state`.
    The device must end up `available`, not stuck at `busy`."""
    from app.appium_nodes.models import AppiumDesiredState, AppiumNode
    from app.devices.services.health import DeviceHealthService

    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="dev-deferred-recovered",
            connection_target="dev-deferred-recovered",
            name="Deferred Recovered",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()

    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4790,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4790,
            pid=0,
            active_connection_target="",
        )
    db_session.add(node)
    session = Session(session_id="sess-deferred-recovered", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    # Defer a stop (simulates an earlier transient failure during this session).
    await _make_real_lifecycle(publisher=event_bus).handle_health_failure(
        db_session, device, source="node_health", reason="Probe failed"
    )

    # Health later recovers - seed derived health to healthy. Recovery wiring
    # would normally clear stop_pending here, but this test exercises the
    # defense-in-depth path where it didn't, so we leave stop_pending=True.)
    _health_svc = DeviceHealthService(publisher=event_bus)
    await _health_svc.apply_node_state_transition(
        db_session,
        device,
        health_running=None,
        health_state=None,
        mark_offline=False,
    )
    await _health_svc.update_device_checks(db_session, device, healthy=True, summary="Healthy")
    await db_session.commit()

    # Session ends — Grid no longer reports it.
    await _sync_sessions_impl(
        db_session,
        settings=FakeSettingsReader({}),
        publisher=AsyncMock(),
        grid=make_fake_grid(_grid_response([])),
        lifecycle=_make_real_lifecycle(),
    )

    await db_session.refresh(device)
    # Intent was cleared but device should be RESTORED to available, not stopped.
    assert device.operational_state == DeviceOperationalState.available
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state["stop_pending"] is False


async def test_sync_does_not_restore_busy_when_fresh_session_inserted_after_precheck(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Race fix: a fresh client session inserted between the outer
    ``still_running`` check and the locked restore must NOT be restored away
    from busy. ``handle_session_finished`` returns ``NO_PENDING`` for
    no-deferred-stop devices without doing the locked Session check, so the
    restore guard performs its own locked recheck.
    """
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="dev-race-restore",
            connection_target="dev-race-restore",
            name="Race Restore",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            # No deferred-stop intent — ``handle_session_finished`` will hit the
            # NO_PENDING fast-path without checking running sessions under lock.
            lifecycle_policy_state={"stop_pending": False, "last_action": "idle"},
        )
    db_session.add(device)
    await db_session.flush()

    # The "old" session that the outer loop will see as ended.
    old_session = Session(session_id="sess-old-ending", device_id=device.id, status=SessionStatus.running)
    db_session.add(old_session)
    await db_session.commit()

    real_lifecycle = _make_real_lifecycle()
    real_handle = real_lifecycle.handle_session_finished

    async def _handle_then_insert_fresh(db: AsyncSession, dev: Device) -> object:
        # Simulate: between the outer running-set probe and the restore guard,
        # a fresh client session is inserted (e.g. a new POST /api/sessions
        # arriving on a different worker). The new session is committed so
        # the locked recheck inside the restore guard observes it.
        outcome = await real_handle(db, dev)
        new_session = Session(session_id="sess-new-fresh", device_id=dev.id, status=SessionStatus.running)
        db.add(new_session)
        await db.commit()
        return outcome

    real_lifecycle.handle_session_finished = _handle_then_insert_fresh  # type: ignore[method-assign]

    # Old session leaves the Grid (not in active map), triggering ended-session processing.
    svc = SessionSyncService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        grid=make_fake_grid(_grid_response([])),
        lifecycle=real_lifecycle,
    )
    monkeypatch.setattr(svc, "_sweep_stale_stop_pending", AsyncMock())
    await svc.sync(db_session)

    await db_session.refresh(device)
    # The race-prone restore would have moved the device to ``available``.
    # Correct behavior: leave it ``busy`` for the fresh session.
    assert device.operational_state == DeviceOperationalState.busy

    # Sanity check the simulated fresh session is the reason.
    fresh = await db_session.execute(select(Session).where(Session.session_id == "sess-new-fresh"))
    assert fresh.scalar_one_or_none() is not None


async def test_sync_does_not_track_probe_sessions(db_session: AsyncSession, db_host: Host) -> None:
    """Probe sessions are filtered out and never persisted as real Session rows."""
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="dev-probe",
            connection_target="dev-probe",
            name="Probe Phone",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()

    grid_data = _grid_response(
        [
            {
                "sessionId": "probe-sess-1",
                "capabilities": {
                    "platformName": "android",
                    "appium:udid": "dev-probe",
                    "gridfleet:probeSession": True,
                    "gridfleet:testName": "__gridfleet_probe__",
                },
            }
        ]
    )

    await _sync_sessions_impl(
        db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(grid_data)
    )

    result = await db_session.execute(select(Session))
    sessions = result.scalars().all()
    assert sessions == []

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available


async def test_sync_skips_probe_slot_when_device_marked_inflight(db_session: AsyncSession, db_host: Host) -> None:
    """Probe Grid slots whose caps Appium stripped are filtered via the inflight registry.

    The Appium driver does not echo client-supplied ``gridfleet:*`` markers
    back in matched capabilities, so a viability probe's slot looks like an
    ordinary session in the Grid /status payload. The probe runner registers
    the device id in ``probe_inflight`` for the lifetime of the Grid session;
    session_sync must skip those slots so no phantom Session row is created.
    """
    from app.sessions import probe_inflight

    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="dev-stripped-probe",
            connection_target="dev-stripped-probe",
            name="Stripped Probe Phone",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()

    # No gridfleet:testName / gridfleet:probeSession in caps — mirrors what
    # the Appium driver actually returns for a real viability probe.
    grid_data = _grid_response([_grid_session("real-uuid-probe", "dev-stripped-probe", device_id=str(device.id))])

    probe_inflight.mark_probe_started(str(device.id))
    try:
        await _sync_sessions_impl(
            db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(grid_data)
        )
    finally:
        probe_inflight.mark_probe_finished(str(device.id))

    result = await db_session.execute(select(Session))
    assert result.scalars().all() == []


async def test_sync_ignores_reserved_placeholder_sessions(db_session: AsyncSession, db_host: Host) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="dev-reserved",
            connection_target="emulator-5554",
            name="Reserved Placeholder Phone",
            os_version="14",
            host_id=db_host.id,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.emulator,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()

    grid_data = _grid_response([_grid_session("reserved", "emulator-5554")])

    await _sync_sessions_impl(
        db_session, settings=FakeSettingsReader({}), publisher=AsyncMock(), grid=make_fake_grid(grid_data)
    )

    result = await db_session.execute(select(Session).where(Session.session_id == "reserved"))
    assert result.scalar_one_or_none() is None
    await db_session.refresh(device)
    assert device.operational_state != DeviceOperationalState.maintenance


async def test_sweep_clears_stale_stop_pending_for_devices_without_sessions(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-stuck-stop-sweep",
            connection_target="policy-stuck-stop-sweep",
            name="Stuck Deferred Stop Sweep Device",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    session = Session(
        session_id="sess-stuck-stop-sweep",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    result = await _make_real_lifecycle(publisher=event_bus).handle_health_failure(
        db_session, device, source="device_checks", reason="ADB not responsive"
    )
    assert result == "deferred"

    # Simulate the historical bug: a session ended directly in the DB without the helper.
    session.status = SessionStatus.passed
    session.ended_at = datetime.now(UTC)
    await db_session.commit()

    await db_session.refresh(device)
    assert device.lifecycle_policy_state["stop_pending"] is True

    svc = SessionSyncService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        grid=make_fake_grid({"value": {"ready": True, "nodes": []}}),
        lifecycle=_make_real_lifecycle(),
    )
    await svc.sync(db_session)

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False


async def test_sweep_runs_when_grid_is_unreachable(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runbook promises one-poll healing for stale ``stop_pending`` rows.

    Tying the sweep to Grid availability would silently weaken that guarantee
    during Grid outages, when stale rows still need to be healed because the
    sweep relies on DB state only. Audit P2 — sweep must run independent of
    Grid status.
    """
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-sweep-grid-down",
            connection_target="policy-sweep-grid-down",
            name="Sweep Grid Down",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.flush()
    session = Session(
        session_id="sess-sweep-grid-down",
        device_id=device.id,
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    result = await _make_real_lifecycle(publisher=event_bus).handle_health_failure(
        db_session, device, source="device_checks", reason="ADB hung"
    )
    assert result == "deferred"

    session.status = SessionStatus.passed
    session.ended_at = datetime.now(UTC)
    await db_session.commit()

    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state["stop_pending"] is True

    # Shape that triggers the early-return branch in _sync_sessions:
    # ready=False AND an "error" key present.
    svc = SessionSyncService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        grid=make_fake_grid({"value": {"ready": False}, "error": "connection refused"}),
        lifecycle=_make_real_lifecycle(),
    )
    await svc.sync(db_session)

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False, (
        "sweep must heal stale stop_pending rows even when Grid is unreachable"
    )


def test_session_sync_service_satisfies_protocol() -> None:
    assert issubclass(SessionSyncService, SessionSyncProtocol)
