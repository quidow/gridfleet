from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceReservation, DeviceType
from app.devices.services import state_write_guard
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.runs.models import RunState, TestRun
from app.runs.service_lifecycle import RunLifecycleService
from app.runs.service_lifecycle_release import RunReleaseService
from app.runs.service_reservation import RunReservationService
from app.sessions.models import Session, SessionStatus
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

_settings = FakeSettingsReader({})


async def test_force_release_clears_stop_pending(
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
            identity_value="policy-stuck-stop-3",
            connection_target="policy-stuck-stop-3",
            name="Stuck Deferred Stop Device 3",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    run = TestRun(
        id=uuid4(),
        name="run-stuck-stop-3",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.flush()
    reservation = DeviceReservation(
        run_id=run.id,
        device_id=device.id,
        identity_value=device.identity_value,
        connection_target=device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        os_version=device.os_version,
    )
    db_session.add(reservation)
    with state_write_guard.bypass():
        db_session.add(
            AppiumNode(
                device_id=device.id,
                port=4723,
                desired_state=AppiumDesiredState.running,
                desired_port=4723,
                pid=1,
                active_connection_target="http://10.0.0.1:4723",
            )
        )
    session = Session(
        session_id="sess-stuck-stop-3",
        device_id=device.id,
        run_id=run.id,
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    monkeypatch.setattr(
        "app.runs.service_lifecycle_release.appium_direct.terminate_session",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.runs.service_lifecycle_release.appium_direct.session_alive",
        AsyncMock(return_value=True),
    )

    real_deferred_stop = LifecyclePolicyService(
        review=build_review_service(),
        publisher=event_bus,
        settings=_settings,
        actions=LifecyclePolicyActionsService(
            publisher=event_bus,
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
        viability=Mock(),
        node_manager=AsyncMock(),
    )
    result = await real_deferred_stop.handle_health_failure(
        db_session, device, source="device_checks", reason="ADB not responsive"
    )
    assert result == "deferred"

    test_release = RunReleaseService(
        publisher=event_bus,
        settings=_settings,
        deferred_stop=real_deferred_stop,
    )
    test_lifecycle = RunLifecycleService(publisher=event_bus, settings=_settings, release=test_release)
    await test_lifecycle.force_release(db_session, run.id)

    reloaded = await db_session.get(Device, device.id)
    assert reloaded is not None
    assert reloaded.lifecycle_policy_state is not None
    assert reloaded.lifecycle_policy_state["stop_pending"] is False


async def test_release_devices_defers_lifecycle_cleanup_until_after_commit(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_release_devices`` must NOT invoke
    ``complete_deferred_stop_if_session_ended`` while the run-state transaction
    is open.

    Calling the lifecycle helper inline would surface partial commits on the
    caller's transaction (the helper commits internally via
    ``handle_node_crash``). Audit P1 — collect device IDs during
    ``_release_devices`` and run cleanup only after the run-state commit.
    """
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="policy-release-commit-boundary",
            connection_target="policy-release-commit-boundary",
            name="Release Commit Boundary",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    run = TestRun(
        id=uuid4(),
        name="run-release-commit-boundary",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.flush()
    reservation = DeviceReservation(
        run_id=run.id,
        device_id=device.id,
        identity_value=device.identity_value,
        connection_target=device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        os_version=device.os_version,
    )
    db_session.add(reservation)
    session = Session(
        session_id="sess-release-commit-boundary",
        device_id=device.id,
        run_id=run.id,
        status=SessionStatus.running,
    )
    db_session.add(session)
    await db_session.commit()

    call_log: list[str] = []

    class SpyReleaseService(RunReleaseService):
        async def release_devices(self, *args: object, **kwargs: object) -> list:
            result = await super().release_devices(*args, **kwargs)  # type: ignore[misc]
            call_log.append("release_done")
            return result  # type: ignore[return-value]

    async def _spy_deferred_stop(db: object, device: object) -> object:
        call_log.append("helper")

    spy_deferred_stop = AsyncMock()
    spy_deferred_stop.complete_deferred_stop_if_session_ended = _spy_deferred_stop

    spy_release = SpyReleaseService(
        publisher=event_bus,
        settings=_settings,
        deferred_stop=spy_deferred_stop,
    )
    spy_lifecycle = RunLifecycleService(publisher=event_bus, settings=_settings, release=spy_release)

    await spy_lifecycle.force_release(db_session, run.id)

    # release_devices must complete strictly before the lifecycle helper is
    # invoked on any device — otherwise the helper's internal commits could
    # leak under the run-state transaction.
    assert "release_done" in call_log
    assert "helper" in call_log
    assert call_log.index("release_done") < call_log.index("helper"), call_log


async def test_terminate_and_probe_survivors_classifies_alive_vs_gone(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe pass returns ONLY the device whose session is still alive after
    the DELETE. A 404/gone session (session_alive -> False) is not a survivor."""
    survivor_dev, gone_dev = None, None
    with state_write_guard.bypass():
        survivor_dev = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-survivor",
            connection_target="probe-survivor",
            name="Probe Survivor",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
        gone_dev = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-gone",
            connection_target="probe-gone",
            name="Probe Gone",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add_all([survivor_dev, gone_dev])
    run = TestRun(
        id=uuid4(),
        name="probe-run",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.flush()
    for dev in (survivor_dev, gone_dev):
        db_session.add(
            DeviceReservation(
                run_id=run.id,
                device_id=dev.id,
                identity_value=dev.identity_value,
                connection_target=dev.connection_target,
                pack_id=dev.pack_id,
                platform_id=dev.platform_id,
                os_version=dev.os_version,
            )
        )
        with state_write_guard.bypass():
            db_session.add(
                AppiumNode(
                    device_id=dev.id,
                    port=4723,
                    desired_state=AppiumDesiredState.running,
                    desired_port=4723,
                    pid=1,
                    active_connection_target="http://10.0.0.1:4723",
                )
            )
        db_session.add(
            Session(
                session_id=f"sess-{dev.identity_value}",
                device_id=dev.id,
                run_id=run.id,
                status=SessionStatus.running,
            )
        )
    await db_session.commit()

    monkeypatch.setattr(
        "app.runs.service_lifecycle_release.appium_direct.terminate_session",
        AsyncMock(return_value=True),
    )

    async def fake_alive(target: str, session_id: str, **_: object) -> bool:
        return session_id == "sess-probe-survivor"  # alive for survivor, gone for the other

    monkeypatch.setattr("app.runs.service_lifecycle_release.appium_direct.session_alive", fake_alive)

    release_svc = RunReleaseService(publisher=event_bus, settings=_settings, deferred_stop=AsyncMock())
    refreshed = await db_session.get(TestRun, run.id)
    assert refreshed is not None
    survivors = await release_svc.terminate_run_sessions_and_probe_survivors(db_session, refreshed)

    assert survivors == {survivor_dev.id}


async def test_terminate_and_probe_survivors_keeps_unresolvable_target_session(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-safe (regression): a running session whose Appium target is unresolvable
    (no live node target AND no stored router_target) can be neither DELETEd nor probed,
    so it MUST be kept as a survivor — otherwise force-release would skip its hard-stop
    and the live session would leak. The probe/DELETE must not be invoked for it."""
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="probe-notarget",
            connection_target="probe-notarget",
            name="Probe No Target",
            os_version="14",
            host_id=db_host.id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    run = TestRun(
        id=uuid4(),
        name="probe-notarget-run",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        DeviceReservation(
            run_id=run.id,
            device_id=device.id,
            identity_value=device.identity_value,
            connection_target=device.connection_target,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            os_version=device.os_version,
        )
    )
    # No AppiumNode row + no router_target -> resolve_router_target() returns None, i.e.
    # the session's Appium target is unresolvable (mirrors a node whose live target is
    # gone with no allocation-time fallback stored).
    db_session.add(
        Session(
            session_id="sess-probe-notarget",
            device_id=device.id,
            run_id=run.id,
            status=SessionStatus.running,
        )
    )
    await db_session.commit()

    # Neither call may fire for an unresolvable target — blow up if they do, proving the
    # device is classified a survivor via the fail-safe, not via a probe.
    monkeypatch.setattr(
        "app.runs.service_lifecycle_release.appium_direct.terminate_session",
        AsyncMock(side_effect=AssertionError("terminate_session called for unresolvable target")),
    )
    monkeypatch.setattr(
        "app.runs.service_lifecycle_release.appium_direct.session_alive",
        AsyncMock(side_effect=AssertionError("session_alive called for unresolvable target")),
    )

    release_svc = RunReleaseService(publisher=event_bus, settings=_settings, deferred_stop=AsyncMock())
    refreshed = await db_session.get(TestRun, run.id)
    assert refreshed is not None
    survivors = await release_svc.terminate_run_sessions_and_probe_survivors(db_session, refreshed)

    assert survivors == {device.id}


async def _seed_force_release_fixture(db_session: AsyncSession, host_id: object, suffix: str) -> tuple:  # type: ignore[type-arg]
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value=f"fr-{suffix}",
            connection_target=f"fr-{suffix}",
            name=f"Force Release {suffix}",
            os_version="14",
            host_id=host_id,
            operational_state=DeviceOperationalState.busy,
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
            # verified_at required so device_in_service() is True, which causes the
            # reconciler to synthesize a baseline:idle (priority 10) node-start intent
            # after run intents are revoked — this is the P3 warm-park benefit. Without
            # it, no baseline:idle is synthesized and the node stops via the no-intent
            # path, masking the warm path entirely.
            verified_at=datetime.now(UTC),
        )
    db_session.add(device)
    run = TestRun(
        id=uuid4(),
        name=f"fr-run-{suffix}",
        state=RunState.active,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
        last_heartbeat=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        DeviceReservation(
            run_id=run.id,
            device_id=device.id,
            identity_value=device.identity_value,
            connection_target=device.connection_target,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            os_version=device.os_version,
        )
    )
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=1,
            active_connection_target="http://10.0.0.1:4723",
        )
    db_session.add(node)
    db_session.add(
        Session(session_id=f"sess-fr-{suffix}", device_id=device.id, run_id=run.id, status=SessionStatus.running)
    )
    await db_session.commit()
    return device, run, node


async def test_force_release_keeps_node_warm_when_session_cleanly_gone(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3: the DELETE removed the session (session_alive -> False), so force-release
    registers NO forced_release hard-stop — the node stays desired=running (warm),
    and the FORCED_RELEASE_NODE_STOP_TOTAL counter does not move."""
    from app.core import metrics_recorders

    _device, run, node = await _seed_force_release_fixture(db_session, db_host.id, "warm")
    monkeypatch.setattr(
        "app.runs.service_lifecycle_release.appium_direct.terminate_session", AsyncMock(return_value=True)
    )
    monkeypatch.setattr("app.runs.service_lifecycle_release.appium_direct.session_alive", AsyncMock(return_value=False))
    before = metrics_recorders.FORCED_RELEASE_NODE_STOP_TOTAL._value.get()

    lifecycle = RunLifecycleService(
        publisher=event_bus,
        settings=_settings,
        release=RunReleaseService(publisher=event_bus, settings=_settings, deferred_stop=AsyncMock()),
    )
    await lifecycle.force_release(db_session, run.id)

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.running  # never stopped -> no cold restart
    assert metrics_recorders.FORCED_RELEASE_NODE_STOP_TOTAL._value.get() == before

    from sqlalchemy import select as _select

    sess_row = (await db_session.execute(_select(Session).where(Session.session_id == "sess-fr-warm"))).scalar_one()
    assert sess_row.status == SessionStatus.error  # force-released sessions must be error, not passed


async def test_force_release_hard_stops_when_session_survives(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3: the session is still alive after the DELETE (session_alive -> True), so
    force-release registers the forced_release hard-stop -> node desired=stopped,
    the counter increments, and the (un-DELETEd) session row is left running for the
    idle reaper while the node hard-stop tears it down."""
    from app.core import metrics_recorders

    _device, run, node = await _seed_force_release_fixture(db_session, db_host.id, "stop")
    # A genuine survivor: the W3C DELETE did NOT take (terminate_session -> False) yet the
    # session is still alive (session_alive -> True). "DELETE succeeded AND still alive"
    # is physically contradictory and would close the row, never exercising the real
    # leave-running path — so use the realistic failed-DELETE survivor.
    monkeypatch.setattr(
        "app.runs.service_lifecycle_release.appium_direct.terminate_session", AsyncMock(return_value=False)
    )
    monkeypatch.setattr("app.runs.service_lifecycle_release.appium_direct.session_alive", AsyncMock(return_value=True))
    before = metrics_recorders.FORCED_RELEASE_NODE_STOP_TOTAL._value.get()

    lifecycle = RunLifecycleService(
        publisher=event_bus,
        settings=_settings,
        release=RunReleaseService(publisher=event_bus, settings=_settings, deferred_stop=AsyncMock()),
    )
    await lifecycle.force_release(db_session, run.id)

    await db_session.refresh(node)
    assert node.desired_state == AppiumDesiredState.stopped
    assert metrics_recorders.FORCED_RELEASE_NODE_STOP_TOTAL._value.get() == before + 1

    from sqlalchemy import select as _select

    # DELETE failed, so the close loop leaves the row running (idle reaper backstops).
    sess_row = (await db_session.execute(_select(Session).where(Session.session_id == "sess-fr-stop"))).scalar_one()
    assert sess_row.status == SessionStatus.running
