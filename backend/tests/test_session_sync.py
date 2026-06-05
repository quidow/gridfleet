"""Observation sweep semantics for ``SessionSyncService`` (hub-free).

The sweep probes each device's Appium server directly via
``app.grid.appium_direct``:

* DB-truth running sessions are checked with ``session_alive`` — dead ones are
  closed and the device freed; indeterminate verdicts are left untouched.
* Each running node is enumerated with ``list_sessions`` — sessions with no
  tracking DB row (and no in-flight probe) are terminated.

The sweep never creates or hydrates Session rows: row creation is owned by the
allocation API.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services import state_write_guard
from app.hosts.models import Host
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.runs.service_reservation import RunReservationService
from app.sessions import probe_inflight, service_sync
from app.sessions.models import Session, SessionStatus
from app.sessions.protocols import SessionSyncProtocol
from app.sessions.service_sync import SessionSyncService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _make_real_lifecycle(publisher: object = None) -> LifecyclePolicyService:
    pub = publisher if publisher is not None else event_bus
    return LifecyclePolicyService(
        review=build_review_service(),
        publisher=pub,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=pub,
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
        viability=Mock(),
        node_manager=AsyncMock(),
    )


def _make_sync_service(lifecycle: object | None = None) -> SessionSyncService:
    return SessionSyncService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        lifecycle=lifecycle if lifecycle is not None else AsyncMock(),
    )


async def _seed_device_with_node(
    db: AsyncSession,
    host: Host,
    *,
    identity_value: str,
    operational_state: DeviceOperationalState,
    port: int = 4723,
    desired_state: AppiumDesiredState = AppiumDesiredState.running,
) -> Device:
    """Seed a Device + AppiumNode so ``node_target`` resolves to host.ip:port."""
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value=identity_value,
            connection_target=identity_value,
            name=f"Device {identity_value}",
            os_version="14",
            host_id=host.id,
            operational_state=operational_state,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db.add(device)
    await db.flush()
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=port,
            grid_url="http://hub:4444",
            desired_state=desired_state,
            desired_port=port if desired_state is AppiumDesiredState.running else None,
            pid=42,
            active_connection_target=device.connection_target,
        )
    db.add(node)
    await db.flush()
    return device


@pytest.fixture(autouse=True)
def _skip_leader_fencing() -> Iterator[None]:
    """No-op assert_current_leader so unit tests don't need a real leader row."""
    with patch("app.sessions.service_sync.assert_current_leader"):
        yield


@pytest.fixture(autouse=True)
def _stub_appium_direct(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Default: every session alive, no node enumeration, no terminations.

    Tests override individual entries to drive specific branches.
    """
    state: dict[str, Any] = {
        "alive": {},  # session_id -> True | False | None (default True)
        "list": {},  # target -> list[str] | None (default None)
        "terminated": [],  # (target, session_id) tuples
    }

    async def fake_session_alive(target: str, session_id: str, **_: object) -> bool | None:
        return state["alive"].get(session_id, True)

    async def fake_list_sessions(target: str, **_: object) -> list[str] | None:
        return state["list"].get(target)

    async def fake_terminate(target: str, session_id: str, **_: object) -> bool:
        state["terminated"].append((target, session_id))
        return True

    monkeypatch.setattr(service_sync.appium_direct, "session_alive", fake_session_alive)
    monkeypatch.setattr(service_sync.appium_direct, "list_sessions", fake_list_sessions)
    monkeypatch.setattr(service_sync.appium_direct, "terminate_session", fake_terminate)
    return state


# --------------------------------------------------------------------------- #
# Liveness                                                                     #
# --------------------------------------------------------------------------- #


async def test_alive_session_left_untouched(db_session: AsyncSession, db_host: Host) -> None:
    device = await _seed_device_with_node(
        db_session, db_host, identity_value="live-1", operational_state=DeviceOperationalState.busy
    )
    session = Session(session_id="sess-live", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    await _make_sync_service().sync(db_session)

    await db_session.refresh(session)
    assert session.status == SessionStatus.running
    assert session.ended_at is None


async def test_dead_session_closed_and_device_freed(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    device = await _seed_device_with_node(
        db_session, db_host, identity_value="dead-1", operational_state=DeviceOperationalState.busy
    )
    session = Session(session_id="sess-dead", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    _stub_appium_direct["alive"]["sess-dead"] = False
    publisher = Mock()
    await SessionSyncService(publisher=publisher, settings=FakeSettingsReader({}), lifecycle=AsyncMock()).sync(
        db_session
    )

    await db_session.refresh(session)
    assert session.status == SessionStatus.passed
    assert session.ended_at is not None
    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available
    # The session.ended event was queued.
    ended_calls = [c for c in publisher.queue_for_session.call_args_list if c.args[1] == "session.ended"]
    assert len(ended_calls) >= 1


async def test_indeterminate_session_left_alone(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    device = await _seed_device_with_node(
        db_session, db_host, identity_value="indet-1", operational_state=DeviceOperationalState.busy
    )
    session = Session(session_id="sess-indet", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    _stub_appium_direct["alive"]["sess-indet"] = None
    await _make_sync_service().sync(db_session)

    await db_session.refresh(session)
    assert session.status == SessionStatus.running
    assert session.ended_at is None
    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.busy


async def test_pending_session_never_probed(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """Pending rows are owned by the allocation reaper; the sweep ignores them."""
    device = await _seed_device_with_node(
        db_session, db_host, identity_value="pend-1", operational_state=DeviceOperationalState.busy
    )
    session = Session(session_id="alloc-pending", device_id=device.id, status=SessionStatus.pending)
    db_session.add(session)
    await db_session.commit()

    # If the pending row were probed-and-killed, alive=False would close it.
    _stub_appium_direct["alive"]["alloc-pending"] = False
    await _make_sync_service().sync(db_session)

    await db_session.refresh(session)
    assert session.status == SessionStatus.pending
    assert session.ended_at is None


async def test_dead_session_marks_offline_when_node_stop_pending(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """A dead session on a device with a held graceful-stop intent goes offline."""
    from app.devices.services.intent import IntentService
    from app.devices.services.intent_types import NODE_PROCESS, PRIORITY_HEALTH_FAILURE, IntentRegistration

    device = await _seed_device_with_node(
        db_session, db_host, identity_value="dead-stop", operational_state=DeviceOperationalState.busy
    )
    session = Session(session_id="sess-dead-stop", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    await IntentService(db_session).register_intents(
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

    _stub_appium_direct["alive"]["sess-dead-stop"] = False
    await _make_sync_service().sync(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline


# --------------------------------------------------------------------------- #
# Orphan kill                                                                  #
# --------------------------------------------------------------------------- #


async def test_orphan_session_killed(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    device = await _seed_device_with_node(
        db_session, db_host, identity_value="orph-1", operational_state=DeviceOperationalState.busy
    )
    # One tracked running session and one orphan id reported by the node.
    tracked = Session(session_id="sess-tracked", device_id=device.id, status=SessionStatus.running)
    db_session.add(tracked)
    await db_session.commit()

    target = f"http://{db_host.ip}:4723"
    _stub_appium_direct["list"][target] = ["sess-tracked", "sess-orphan"]
    await _make_sync_service().sync(db_session)

    assert (target, "sess-orphan") in _stub_appium_direct["terminated"]
    assert (target, "sess-tracked") not in _stub_appium_direct["terminated"]


async def test_orphan_spared_when_pending_row_exists(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """A confirm-in-flight session (pending row) must not be killed as an orphan."""
    device = await _seed_device_with_node(
        db_session, db_host, identity_value="orph-pend", operational_state=DeviceOperationalState.busy
    )
    pending = Session(session_id="sess-confirming", device_id=device.id, status=SessionStatus.pending)
    db_session.add(pending)
    await db_session.commit()

    target = f"http://{db_host.ip}:4723"
    _stub_appium_direct["list"][target] = ["sess-confirming"]
    await _make_sync_service().sync(db_session)

    assert _stub_appium_direct["terminated"] == []


async def test_orphan_with_inflight_probe_spared(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    device = await _seed_device_with_node(
        db_session, db_host, identity_value="orph-probe", operational_state=DeviceOperationalState.available
    )
    await db_session.commit()

    target = f"http://{db_host.ip}:4723"
    _stub_appium_direct["list"][target] = ["probe-sess-uuid"]
    probe_inflight.mark_probe_started(str(device.id))
    try:
        await _make_sync_service().sync(db_session)
    finally:
        probe_inflight.mark_probe_finished(str(device.id))

    assert _stub_appium_direct["terminated"] == []


async def test_list_sessions_none_skips_node(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """A node without session_discovery returns None — no enumeration, no kills."""
    await _seed_device_with_node(
        db_session, db_host, identity_value="orph-none", operational_state=DeviceOperationalState.busy
    )
    await db_session.commit()

    target = f"http://{db_host.ip}:4723"
    _stub_appium_direct["list"][target] = None  # default already None, but explicit
    await _make_sync_service().sync(db_session)

    assert _stub_appium_direct["terminated"] == []


async def test_stopped_node_not_enumerated(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """A node whose desired_state is stopped is skipped even if it reports sessions."""
    await _seed_device_with_node(
        db_session,
        db_host,
        identity_value="orph-stopped",
        operational_state=DeviceOperationalState.offline,
        desired_state=AppiumDesiredState.stopped,
    )
    await db_session.commit()

    target = f"http://{db_host.ip}:4723"
    _stub_appium_direct["list"][target] = ["sess-ghost"]
    await _make_sync_service().sync(db_session)

    assert _stub_appium_direct["terminated"] == []


# --------------------------------------------------------------------------- #
# No insertion / hydration                                                     #
# --------------------------------------------------------------------------- #


async def test_sweep_never_inserts_sessions(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """An enumerated orphan is killed, never hydrated into a Session row."""
    device = await _seed_device_with_node(
        db_session, db_host, identity_value="noins-1", operational_state=DeviceOperationalState.busy
    )
    await db_session.commit()

    target = f"http://{db_host.ip}:4723"
    _stub_appium_direct["list"][target] = ["mystery-sess"]
    await _make_sync_service().sync(db_session)

    rows = (await db_session.execute(select(Session).where(Session.device_id == device.id))).scalars().all()
    assert rows == []


# --------------------------------------------------------------------------- #
# Stale stop_pending sweep                                                     #
# --------------------------------------------------------------------------- #


async def test_sweep_clears_stale_stop_pending_for_devices_without_sessions(
    db_session: AsyncSession, db_host: Host
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
    session = Session(session_id="sess-stuck-stop-sweep", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    result = await _make_real_lifecycle(publisher=event_bus).handle_health_failure(
        db_session, device, source="device_checks", reason="ADB not responsive"
    )
    assert result == "deferred"

    # Simulate the historical bug: a session ended directly in the DB.
    session.status = SessionStatus.passed
    session.ended_at = datetime.now(UTC)
    await db_session.commit()

    await db_session.refresh(device)
    assert device.lifecycle_policy_state["stop_pending"] is True

    await _make_sync_service(lifecycle=_make_real_lifecycle()).sync(db_session)

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False


def test_session_sync_service_satisfies_protocol() -> None:
    assert issubclass(SessionSyncService, SessionSyncProtocol)
