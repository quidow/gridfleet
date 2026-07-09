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

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.runs.service_reservation import RunReservationService
from app.sessions import probe_inflight, service_sync
from app.sessions.models import Session, SessionStatus
from app.sessions.service_sync import SessionSyncService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

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
    node = AppiumNode(
        device_id=device.id,
        port=port,
        desired_state=desired_state,
        desired_port=port if desired_state is AppiumDesiredState.running else None,
        pid=42,
        active_connection_target=device.connection_target,
    )
    db.add(node)
    await db.flush()
    return device


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


async def test_operator_stopped_node_session_closed(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """C2: a running session whose device's node has desired_state == stopped (an
    operator stopped the node out from under the live session) is closed and the device
    freed, even though the probe over a stopped process is indeterminate. Operator intent
    is unambiguous."""
    device = await _seed_device_with_node(
        db_session,
        db_host,
        identity_value="opstop-1",
        operational_state=DeviceOperationalState.busy,
        desired_state=AppiumDesiredState.stopped,
    )
    session = Session(session_id="sess-opstop", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    # Probe is indeterminate (connection refused on a stopped process) — without the
    # node-state input the session would be left running.
    _stub_appium_direct["alive"]["sess-opstop"] = None
    target = f"http://{db_host.ip}:4723"
    before = service_sync.GRID_NODE_STOPPED_SESSIONS_CLOSED_TOTAL._value.get()
    await _make_sync_service(lifecycle=_make_real_lifecycle()).sync(db_session)
    after = service_sync.GRID_NODE_STOPPED_SESSIONS_CLOSED_TOTAL._value.get()

    assert (target, "sess-opstop") in _stub_appium_direct["terminated"]
    await db_session.refresh(session)
    assert session.status == SessionStatus.passed
    assert session.ended_at is not None
    assert after == before + 1


async def test_desired_running_indeterminate_session_untouched(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """C2: a session whose node is still desired_state == running but whose probe is
    indeterminate (an observed-down node with a respawn possibly in flight) is left
    untouched — the node-stopped close fires only on an unambiguous operator stop."""
    device = await _seed_device_with_node(
        db_session,
        db_host,
        identity_value="desired-run-indet",
        operational_state=DeviceOperationalState.busy,
        desired_state=AppiumDesiredState.running,
    )
    session = Session(session_id="sess-desired-run-indet", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    _stub_appium_direct["alive"]["sess-desired-run-indet"] = None
    await _make_sync_service().sync(db_session)

    assert _stub_appium_direct["terminated"] == []
    await db_session.refresh(session)
    assert session.status == SessionStatus.running
    assert session.ended_at is None


async def test_all_running_sessions_probed_concurrently(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#10: every running session is probed in one sweep (the probes are gathered, not
    skipped). Records the set of probed session ids and asserts all of them appear."""
    import asyncio

    probed: set[str] = set()

    async def recording_alive(target: str, session_id: str, **_: object) -> bool:
        await asyncio.sleep(0)  # yield so gathered probes interleave
        probed.add(session_id)
        return True

    monkeypatch.setattr(service_sync.appium_direct, "session_alive", recording_alive)
    monkeypatch.setattr(service_sync.appium_direct, "list_sessions", AsyncMock(return_value=None))
    monkeypatch.setattr(service_sync.appium_direct, "terminate_session", AsyncMock(return_value=True))

    expected: set[str] = set()
    for i in range(5):
        device = await _seed_device_with_node(
            db_session, db_host, identity_value=f"probe-conc-{i}", operational_state=DeviceOperationalState.busy
        )
        sid = f"sess-conc-{i}"
        db_session.add(Session(session_id=sid, device_id=device.id, status=SessionStatus.running))
        expected.add(sid)
    await db_session.commit()

    await _make_sync_service().sync(db_session)

    assert probed == expected


async def test_dead_session_marks_offline_when_node_stop_pending(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """A dead session on a device with a held graceful-stop intent goes offline."""
    from app.devices.services.intent import IntentService
    from app.devices.services.intent_types import NODE_PROCESS, IntentRegistration

    device = await _seed_device_with_node(
        db_session, db_host, identity_value="dead-stop", operational_state=DeviceOperationalState.busy
    )
    session = Session(session_id="sess-dead-stop", device_id=device.id, status=SessionStatus.running)
    db_session.add(session)
    await db_session.commit()

    await IntentService(db_session).register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"health_failure:node:{device.id}",
                axis=NODE_PROCESS,
                payload={"action": "stop"},
            ),
        ],
    )
    await db_session.commit()

    _stub_appium_direct["alive"]["sess-dead-stop"] = False
    await _make_sync_service().sync(db_session)

    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.offline


# --------------------------------------------------------------------------- #
# Idle reaper (#3)                                                             #
# --------------------------------------------------------------------------- #


def _idle_sync_service(
    idle_timeout_sec: int, *, first_command_grace_sec: int = 180, idle_ceiling_sec: int = 7200
) -> SessionSyncService:
    return SessionSyncService(
        publisher=event_bus,
        settings=FakeSettingsReader(
            {
                "grid.session_idle_timeout_sec": idle_timeout_sec,
                "grid.session_first_command_grace_sec": first_command_grace_sec,
                "grid.session_idle_timeout_ceiling_sec": idle_ceiling_sec,
            }
        ),
        lifecycle=AsyncMock(),
    )


async def test_idle_session_over_threshold_reaped(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """A running session whose last activity is older than the idle timeout is
    terminated on the node, closed, the device freed, and the counter advances."""
    device = await _seed_device_with_node(
        db_session, db_host, identity_value="idle-1", operational_state=DeviceOperationalState.busy
    )
    session = Session(
        session_id="sess-idle",
        device_id=device.id,
        status=SessionStatus.running,
        last_activity_at=datetime.now(UTC) - timedelta(seconds=120),
    )
    db_session.add(session)
    await db_session.commit()

    target = f"http://{db_host.ip}:4723"
    before = service_sync.GRID_IDLE_SESSIONS_REAPED_TOTAL._value.get()
    await _idle_sync_service(idle_timeout_sec=60).sync(db_session)
    after = service_sync.GRID_IDLE_SESSIONS_REAPED_TOTAL._value.get()

    assert (target, "sess-idle") in _stub_appium_direct["terminated"]
    await db_session.refresh(session)
    assert session.status == SessionStatus.passed
    assert session.ended_at is not None
    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available
    assert after == before + 1


async def test_idle_session_terminate_failure_defers_close(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wave-5 #3: a reap whose Appium DELETE fails (transient 5xx/timeout) must NOT
    close the DB row — the session may still be alive on the node, and closing would
    free the device for re-allocation under the live foreign session. The row stays
    running and the reap retries next tick (terminate_session returns True on 404,
    so an already-gone session still converges to close)."""
    device = await _seed_device_with_node(
        db_session, db_host, identity_value="idle-termfail", operational_state=DeviceOperationalState.busy
    )
    session = Session(
        session_id="sess-idle-termfail",
        device_id=device.id,
        status=SessionStatus.running,
        last_activity_at=datetime.now(UTC) - timedelta(seconds=120),
    )
    db_session.add(session)
    await db_session.commit()

    async def fake_terminate_fail(target: str, session_id: str, **_: object) -> bool:
        return False

    monkeypatch.setattr(service_sync.appium_direct, "terminate_session", fake_terminate_fail)

    before = service_sync.GRID_IDLE_SESSIONS_REAPED_TOTAL._value.get()
    await _idle_sync_service(idle_timeout_sec=60).sync(db_session)

    await db_session.refresh(session)
    assert session.status == SessionStatus.running
    assert session.ended_at is None
    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.busy
    assert service_sync.GRID_IDLE_SESSIONS_REAPED_TOTAL._value.get() == before


async def test_recently_active_session_skips_liveness_probe(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wave-5 #19: the router flushes last_activity_at every ~10s while traffic
    flows, so a session active within the freshness window is provably alive —
    the sweep must not spend one GET per running session per tick re-verifying
    it. Sessions with stale or NULL activity are still probed."""
    fresh_device = await _seed_device_with_node(
        db_session, db_host, identity_value="probe-fresh", operational_state=DeviceOperationalState.busy
    )
    stale_device = await _seed_device_with_node(
        db_session, db_host, identity_value="probe-stale", operational_state=DeviceOperationalState.busy
    )
    fresh = Session(
        session_id="sess-fresh",
        device_id=fresh_device.id,
        status=SessionStatus.running,
        last_activity_at=datetime.now(UTC),
    )
    stale = Session(
        session_id="sess-stale",
        device_id=stale_device.id,
        status=SessionStatus.running,
        last_activity_at=datetime.now(UTC) - timedelta(seconds=120),
    )
    db_session.add_all([fresh, stale])
    await db_session.commit()

    probed: list[str] = []

    async def recording_session_alive(target: str, session_id: str, **_: object) -> bool | None:
        probed.append(session_id)
        return True

    monkeypatch.setattr(service_sync.appium_direct, "session_alive", recording_session_alive)
    # idle_timeout large so the stale session is probed, not reaped.
    await _idle_sync_service(idle_timeout_sec=86400).sync(db_session)

    assert "sess-fresh" not in probed
    assert "sess-stale" in probed
    await db_session.refresh(fresh)
    assert fresh.status == SessionStatus.running


async def test_idle_session_under_threshold_untouched(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """A session that reported activity within the idle window is left running."""
    device = await _seed_device_with_node(
        db_session, db_host, identity_value="idle-2", operational_state=DeviceOperationalState.busy
    )
    session = Session(
        session_id="sess-fresh",
        device_id=device.id,
        status=SessionStatus.running,
        last_activity_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    db_session.add(session)
    await db_session.commit()

    await _idle_sync_service(idle_timeout_sec=60).sync(db_session)

    assert _stub_appium_direct["terminated"] == []
    await db_session.refresh(session)
    assert session.status == SessionStatus.running
    assert session.ended_at is None


async def test_never_commanded_session_over_grace_reaped(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """A session that never reported activity (null last_activity_at) and whose
    started_at (claim time) is older than the first-command grace is terminated on the
    node, closed, the device freed, and the never-commanded counter (not the idle one)
    advances."""
    device = await _seed_device_with_node(
        db_session, db_host, identity_value="grace-1", operational_state=DeviceOperationalState.busy
    )
    session = Session(
        session_id="sess-never-commanded",
        device_id=device.id,
        status=SessionStatus.running,
        started_at=datetime.now(UTC) - timedelta(seconds=200),
        last_activity_at=None,
    )
    db_session.add(session)
    await db_session.commit()

    target = f"http://{db_host.ip}:4723"
    before_grace = service_sync.GRID_NEVER_COMMANDED_SESSIONS_REAPED_TOTAL._value.get()
    before_idle = service_sync.GRID_IDLE_SESSIONS_REAPED_TOTAL._value.get()
    # idle_timeout deliberately huge so only the grace path can fire.
    await _idle_sync_service(idle_timeout_sec=86400, first_command_grace_sec=180).sync(db_session)
    after_grace = service_sync.GRID_NEVER_COMMANDED_SESSIONS_REAPED_TOTAL._value.get()
    after_idle = service_sync.GRID_IDLE_SESSIONS_REAPED_TOTAL._value.get()

    assert (target, "sess-never-commanded") in _stub_appium_direct["terminated"]
    await db_session.refresh(session)
    assert session.status == SessionStatus.passed
    assert session.ended_at is not None
    await db_session.refresh(device)
    assert device.operational_state == DeviceOperationalState.available
    assert after_grace == before_grace + 1
    assert after_idle == before_idle


async def test_never_commanded_session_under_grace_untouched(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """A never-commanded session whose started_at is within the grace is left running —
    a freshly created session that has not yet issued its first command."""
    device = await _seed_device_with_node(
        db_session, db_host, identity_value="grace-2", operational_state=DeviceOperationalState.busy
    )
    session = Session(
        session_id="sess-fresh-noactivity",
        device_id=device.id,
        status=SessionStatus.running,
        started_at=datetime.now(UTC) - timedelta(seconds=30),
        last_activity_at=None,
    )
    db_session.add(session)
    await db_session.commit()

    await _idle_sync_service(idle_timeout_sec=86400, first_command_grace_sec=180).sync(db_session)

    assert _stub_appium_direct["terminated"] == []
    await db_session.refresh(session)
    assert session.status == SessionStatus.running
    assert session.ended_at is None


async def test_old_started_at_with_recent_activity_survives(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """The grace never applies once activity is observed: a session whose started_at is
    well past the grace but whose last_activity_at is recent ages only against the idle
    timeout, so it survives."""
    device = await _seed_device_with_node(
        db_session, db_host, identity_value="grace-3", operational_state=DeviceOperationalState.busy
    )
    session = Session(
        session_id="sess-old-but-active",
        device_id=device.id,
        status=SessionStatus.running,
        started_at=datetime.now(UTC) - timedelta(seconds=1000),
        last_activity_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    db_session.add(session)
    await db_session.commit()

    await _idle_sync_service(idle_timeout_sec=60, first_command_grace_sec=180).sync(db_session)

    assert _stub_appium_direct["terminated"] == []
    await db_session.refresh(session)
    assert session.status == SessionStatus.running
    assert session.ended_at is None


async def test_idle_session_no_node_target_defers(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """C3: an idle session whose device has NO resolvable Appium target (no node row
    and no stored router_target) must DEFER — closing the DB row blind would orphan a
    possibly-still-live Appium session and let the device be re-allocated while the
    session keeps holding it. The row stays running for a later tick when a target
    resolves."""
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="idle-nonode",
        connection_target="idle-nonode",
        name="Idle No Node",
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
        session_id="sess-idle-nonode",
        device_id=device.id,
        status=SessionStatus.running,
        last_activity_at=datetime.now(UTC) - timedelta(seconds=120),
    )
    db_session.add(session)
    await db_session.commit()

    await _idle_sync_service(idle_timeout_sec=60).sync(db_session)

    assert _stub_appium_direct["terminated"] == []  # no target to terminate against
    await db_session.refresh(session)
    assert session.ended_at is None
    assert session.status == SessionStatus.running


async def test_idle_session_port_resolves_via_stored_router_target(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """C3: an idle session whose device's live node target is unresolvable (no node row)
    but which has a router_target stored at allocation is terminated via that stored
    target and the DB row is closed — the reap now uses resolve_router_target's fallback,
    matching every other consumer."""
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="idle-stored-target",
        connection_target="idle-stored-target",
        name="Idle Stored Target",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    stored_target = f"http://{db_host.ip}:4799"
    session = Session(
        session_id="sess-idle-stored",
        device_id=device.id,
        status=SessionStatus.running,
        last_activity_at=datetime.now(UTC) - timedelta(seconds=120),
        router_target=stored_target,
    )
    db_session.add(session)
    await db_session.commit()

    await _idle_sync_service(idle_timeout_sec=60).sync(db_session)

    assert (stored_target, "sess-idle-stored") in _stub_appium_direct["terminated"]
    await db_session.refresh(session)
    assert session.ended_at is not None
    assert session.status == SessionStatus.passed


async def test_non_idle_session_without_node_target_left_alone(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """A fresh (non-idle) running session on a device with no node target has nothing to
    probe; it is left running, not closed."""
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="fresh-nonode",
        connection_target="fresh-nonode",
        name="Fresh No Node",
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
        session_id="sess-fresh-nonode",
        device_id=device.id,
        status=SessionStatus.running,
        last_activity_at=datetime.now(UTC),
    )
    db_session.add(session)
    await db_session.commit()

    await _idle_sync_service(idle_timeout_sec=60).sync(db_session)

    await db_session.refresh(session)
    assert session.status == SessionStatus.running
    assert session.ended_at is None


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


async def test_orphan_spared_during_alloc_confirm_window(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """The allocate→confirm window: the pending row holds an ``alloc-`` placeholder
    while the real Appium id is live. The live id is absent from known_ids, so the
    sweep would kill the in-creation session. On a pending device every id not
    proven doomed (no terminal row) is spared."""
    device = await _seed_device_with_node(
        db_session, db_host, identity_value="orph-window", operational_state=DeviceOperationalState.busy
    )
    pending = Session(session_id="alloc-placeholder-uuid", device_id=device.id, status=SessionStatus.pending)
    db_session.add(pending)
    await db_session.commit()

    target = f"http://{db_host.ip}:4723"
    # The freshly-created Appium session, whose id never matches the placeholder.
    _stub_appium_direct["list"][target] = ["real-appium-sess-id"]
    await _make_sync_service().sync(db_session)

    assert _stub_appium_direct["terminated"] == []


async def test_orphan_sweep_skips_device_with_over_age_pending_row(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """F7: unknown live ids on a device with ANY pending row are spared regardless of
    the row's age. Expiring stale pending rows is the allocation reaper's job (claim
    window + confirm grace); the sweep must not race it by killing a session whose
    confirm is still in flight on an over-age row."""
    device = await _seed_device_with_node(
        db_session, db_host, identity_value="orph-overage", operational_state=DeviceOperationalState.busy
    )
    stale_pending = Session(
        session_id="alloc-stale-uuid",
        device_id=device.id,
        status=SessionStatus.pending,
        started_at=datetime.now(UTC) - timedelta(seconds=600),  # well past the claim window
    )
    db_session.add(stale_pending)
    await db_session.commit()

    target = f"http://{db_host.ip}:4723"
    _stub_appium_direct["list"][target] = ["real-appium-sess-id"]
    await _make_sync_service().sync(db_session)

    assert _stub_appium_direct["terminated"] == []


async def test_orphan_sweep_kills_doomed_id_on_pending_device(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any]
) -> None:
    """Wave-5 #7: a device holding a pending row is no longer spared wholesale. A live
    id matching a TERMINAL row is provably not the in-creation session (which has no
    row until confirm) — e.g. the id stamped by the 409-confirm path after a failed
    router rollback — and is killed even while a new allocation is in flight. Unknown
    ids are still spared (they may be the pending allocation's own session)."""
    device = await _seed_device_with_node(
        db_session, db_host, identity_value="orph-doomed", operational_state=DeviceOperationalState.busy
    )
    pending = Session(session_id="alloc-new-placeholder", device_id=device.id, status=SessionStatus.pending)
    doomed = Session(
        session_id="sess-doomed",
        device_id=device.id,
        status=SessionStatus.error,
        ended_at=datetime.now(UTC),
    )
    db_session.add_all([pending, doomed])
    await db_session.commit()

    target = f"http://{db_host.ip}:4723"
    _stub_appium_direct["list"][target] = ["sess-doomed", "real-in-creation-id"]
    await _make_sync_service().sync(db_session)

    assert _stub_appium_direct["terminated"] == [(target, "sess-doomed")]


async def test_orphan_kill_metric_not_incremented_when_terminate_fails(
    db_session: AsyncSession, db_host: Host, _stub_appium_direct: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orphan-kill counter only advances on a successful termination (#11)."""
    await _seed_device_with_node(
        db_session, db_host, identity_value="orph-metric", operational_state=DeviceOperationalState.busy
    )
    await db_session.commit()

    async def fake_terminate_fail(target: str, session_id: str, **_: object) -> bool:
        return False

    monkeypatch.setattr(service_sync.appium_direct, "terminate_session", fake_terminate_fail)
    target = f"http://{db_host.ip}:4723"
    _stub_appium_direct["list"][target] = ["ghost-sess"]

    before = service_sync.GRID_ORPHAN_SESSIONS_KILLED_TOTAL._value.get()
    await _make_sync_service().sync(db_session)
    after = service_sync.GRID_ORPHAN_SESSIONS_KILLED_TOTAL._value.get()

    assert after == before


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
    """A node without session_discovery returns None — no enumeration, no kills, and
    the unavailable-enumeration counter (#17) increments."""
    await _seed_device_with_node(
        db_session, db_host, identity_value="orph-none", operational_state=DeviceOperationalState.busy
    )
    await db_session.commit()

    target = f"http://{db_host.ip}:4723"
    _stub_appium_direct["list"][target] = None  # default already None, but explicit
    before = service_sync.GRID_ORPHAN_ENUM_UNAVAILABLE_TOTAL._value.get()
    await _make_sync_service().sync(db_session)

    assert _stub_appium_direct["terminated"] == []
    assert service_sync.GRID_ORPHAN_ENUM_UNAVAILABLE_TOTAL._value.get() == before + 1


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


# --------------------------------------------------------------------------- #
# Cap-aware idle reap (7a): pure helpers                                       #
# --------------------------------------------------------------------------- #


def _caps_session(requested: object = None, actual: object = None) -> Session:
    return Session(
        session_id="caps-helper",
        status=SessionStatus.running,
        requested_capabilities=requested,
        actual_capabilities=actual,
    )


def test_new_command_timeout_reads_prefixed_and_bare_keys() -> None:
    assert service_sync._client_new_command_timeout_sec(_caps_session(actual={"appium:newCommandTimeout": 600})) == 600
    assert service_sync._client_new_command_timeout_sec(_caps_session(actual={"newCommandTimeout": 300})) == 300


def test_new_command_timeout_prefers_actual_over_requested() -> None:
    session = _caps_session(requested={"appium:newCommandTimeout": 0}, actual={"appium:newCommandTimeout": 60})
    assert service_sync._client_new_command_timeout_sec(session) == 60


def test_new_command_timeout_falls_back_to_requested() -> None:
    session = _caps_session(requested={"appium:newCommandTimeout": 45}, actual={"platformName": "Android"})
    assert service_sync._client_new_command_timeout_sec(session) == 45


def test_new_command_timeout_rejects_garbage_values() -> None:
    for bad in (True, False, "60", -1, None, [60], {"v": 60}):
        assert (
            service_sync._client_new_command_timeout_sec(_caps_session(actual={"appium:newCommandTimeout": bad}))
            is None
        )
    assert service_sync._client_new_command_timeout_sec(_caps_session()) is None


def test_new_command_timeout_truncates_float() -> None:
    assert service_sync._client_new_command_timeout_sec(_caps_session(actual={"appium:newCommandTimeout": 90.7})) == 90


def test_effective_idle_timeout_formula() -> None:
    def effective(nct: object, *, idle: int = 1800, ceiling: int = 7200) -> int:
        caps = {"appium:newCommandTimeout": nct} if nct is not None else {}
        return service_sync._effective_idle_timeout_sec(_caps_session(actual=caps), idle_timeout=idle, ceiling=ceiling)

    assert effective(None) == 1800  # no client value: operator timeout unchanged
    assert effective(2700) == 2700  # client extends past the operator timeout
    assert effective(0) == 7200  # "never" clamps to the ceiling (N14 guarantee)
    assert effective(99999) == 7200  # huge value clamps to the ceiling
    assert effective(30) == 1800  # client can never SHORTEN the window
    assert effective(0, idle=1800, ceiling=600) == 1800  # misconfigured ceiling never undercuts the operator timeout


# --------------------------------------------------------------------------- #
# Cap-aware idle reap (7a): behavioral                                         #
# --------------------------------------------------------------------------- #


async def _seed_running_session(
    db: AsyncSession,
    host: Host,
    *,
    identity: str,
    session_id: str,
    last_activity_age_sec: int,
    actual_capabilities: dict[str, Any] | None = None,
) -> Session:
    device = await _seed_device_with_node(
        db, host, identity_value=identity, operational_state=DeviceOperationalState.busy
    )
    session = Session(
        session_id=session_id,
        device_id=device.id,
        status=SessionStatus.running,
        last_activity_at=datetime.now(UTC) - timedelta(seconds=last_activity_age_sec),
        actual_capabilities=actual_capabilities,
    )
    db.add(session)
    await db.commit()
    return session


async def test_client_new_command_timeout_extends_idle_window(db_session: AsyncSession, db_host: Host) -> None:
    """Activity past the operator idle timeout but inside the client's larger
    newCommandTimeout is NOT reaped — the client contract is honored (7a)."""
    session = await _seed_running_session(
        db_session,
        db_host,
        identity="nct-extend",
        session_id="sess-nct-extend",
        last_activity_age_sec=300,
        actual_capabilities={"appium:newCommandTimeout": 600},
    )
    await _idle_sync_service(idle_timeout_sec=60).sync(db_session)
    await db_session.refresh(session)
    assert session.status == SessionStatus.running
    assert session.ended_at is None


async def test_extended_window_still_reaps_past_client_timeout(db_session: AsyncSession, db_host: Host) -> None:
    """The extension is a window, not immunity: activity older than the client's
    newCommandTimeout is reaped through the idle path."""
    session = await _seed_running_session(
        db_session,
        db_host,
        identity="nct-expire",
        session_id="sess-nct-expire",
        last_activity_age_sec=700,
        actual_capabilities={"appium:newCommandTimeout": 600},
    )
    before = service_sync.GRID_IDLE_SESSIONS_REAPED_TOTAL._value.get()
    await _idle_sync_service(idle_timeout_sec=60).sync(db_session)
    await db_session.refresh(session)
    assert session.ended_at is not None
    assert service_sync.GRID_IDLE_SESSIONS_REAPED_TOTAL._value.get() == before + 1


async def test_new_command_timeout_zero_clamps_to_ceiling(db_session: AsyncSession, db_host: Host) -> None:
    """newCommandTimeout=0 ('never') does not disable the reap: it clamps to the
    ceiling (N14 zombie guarantee). Past the ceiling -> reaped."""
    session = await _seed_running_session(
        db_session,
        db_host,
        identity="nct-zero",
        session_id="sess-nct-zero",
        last_activity_age_sec=200,
        actual_capabilities={"appium:newCommandTimeout": 0},
    )
    await _idle_sync_service(idle_timeout_sec=60, idle_ceiling_sec=120).sync(db_session)
    await db_session.refresh(session)
    assert session.ended_at is not None


async def test_new_command_timeout_zero_inside_ceiling_survives(db_session: AsyncSession, db_host: Host) -> None:
    """The same 'never' session inside the ceiling survives even though it is
    past the operator idle timeout — proving the extension actually applied."""
    session = await _seed_running_session(
        db_session,
        db_host,
        identity="nct-zero-live",
        session_id="sess-nct-zero-live",
        last_activity_age_sec=90,
        actual_capabilities={"appium:newCommandTimeout": 0},
    )
    await _idle_sync_service(idle_timeout_sec=60, idle_ceiling_sec=600).sync(db_session)
    await db_session.refresh(session)
    assert session.status == SessionStatus.running
    assert session.ended_at is None


async def test_short_new_command_timeout_never_shortens_window(db_session: AsyncSession, db_host: Host) -> None:
    """A client newCommandTimeout BELOW the operator idle timeout does not pull
    the reap earlier: the driver owns short timeouts (S21), the sweep is a backstop."""
    session = await _seed_running_session(
        db_session,
        db_host,
        identity="nct-short",
        session_id="sess-nct-short",
        last_activity_age_sec=100,
        actual_capabilities={"appium:newCommandTimeout": 30},
    )
    await _idle_sync_service(idle_timeout_sec=300).sync(db_session)
    await db_session.refresh(session)
    assert session.status == SessionStatus.running
    assert session.ended_at is None
