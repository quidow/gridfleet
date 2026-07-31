import time
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import ANY, AsyncMock, MagicMock, Mock, patch

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select, text
from sqlalchemy.exc import NoResultFound, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.models.remediation_log import DeviceRemediationLogEntry
from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.health import DeviceHealthService
from app.devices.services.lifecycle_policy_state import set_maintenance_reason
from app.devices.services.lifecycle_policy_state import state as ps
from app.grid.session_create import CREATE_TIMEOUT_MARGIN_SEC, effective_create_timeout
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.runs.service_reservation import RunReservationService
from app.sessions import service_viability as session_viability
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME
from app.sessions.service_probes import PROBE_CHECKED_BY_CAP_KEY
from app.sessions.service_viability import (
    _PROBE_ALWAYS_MATCH_KEYS,
    SessionViabilityProbeInProgressError,
    SessionViabilityProbeNotPermittedError,
    SessionViabilityService,
    _filter_probe_always_match,
    _parse_timestamp,
    _should_run_scheduled_probe,
    get_session_viability,
    grid_probe_response_to_result,
)
from tests.conftest import settings_service
from tests.fakes import FakeSettingsReader
from tests.helpers import (
    create_reservation,
    dispatch_committed_events,
    get_session_viability_control_plane_state,
    set_session_viability_control_plane_entry,
)
from tests.helpers import test_event_bus as _test_event_bus

if TYPE_CHECKING:
    from collections.abc import Iterator

    from app.hosts.models import Host

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")

# Module-level service instance used by local wrappers.
# Tests that need to intercept method calls should patch via
# ``monkeypatch.setattr(_svc, 'probe_session_direct', ...)`` or
# ``patch.object(SessionViabilityService, 'method', ...)``.
_svc = SessionViabilityService(
    publisher=_test_event_bus,
    settings=FakeSettingsReader({}),
    session_factory=AsyncMock(),
    capability=DeviceCapabilityService(),
    health=AsyncMock(),
)


@pytest.fixture(autouse=True)
def _isolate_module_svc() -> Iterator[None]:
    """Restore the shared module-level ``_svc`` state after every test.

    Several tests do ``monkeypatch.setattr(_svc, "probe_session_direct", ...)``.
    Because that method lives on the class, monkeypatch's undo restores it as an
    *instance* attribute on ``_svc`` — a residual that shadows a later test's
    ``patch.object(SessionViabilityService, ...)``, so the real probe runs and a
    passing-probe test flaps the device offline. Snapshotting and restoring
    ``__dict__`` gives each test a clean shared instance. This fixture is autouse
    with no dependencies, so it tears down *after* ``monkeypatch`` undoes its
    changes — clearing whatever residue monkeypatch leaves behind.
    """
    baseline = dict(_svc.__dict__)
    yield
    _svc.__dict__.clear()
    _svc.__dict__.update(baseline)


def _counter_total(name: str) -> float:
    """Current value of an unlabelled counter. These are process-global and
    other tests in the same xdist worker increment them, so every assertion
    below is a delta across one call, never an absolute."""
    return REGISTRY.get_sample_value(name) or 0.0


async def run_session_viability_probe(
    db: AsyncSession,
    device: Device,
    *,
    checked_by: object,
    settings: FakeSettingsReader | None = None,
    retry_after_viability_failure: bool = False,
) -> dict[str, Any]:
    _svc._settings = settings or FakeSettingsReader({})
    # The probe now owns its own fresh sessions via ``_session_factory``; commit
    # the test setup so those fresh sessions can see it, and bind a real
    # sessionmaker to the same engine. No ORM object is carried across the
    # probe's transaction phases — only ``device.id``.
    await db.commit()
    engine = db.bind
    _svc._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return await _svc.run_session_viability_probe(
        device.id, checked_by=checked_by, retry_after_viability_failure=retry_after_viability_failure
    )


async def probe_session_direct(
    capabilities: dict[str, Any],
    timeout_sec: int,
    *,
    target: str | None = None,
    settings: FakeSettingsReader | None = None,
) -> tuple[bool, str | None]:
    svc = SessionViabilityService(
        publisher=Mock(),
        settings=settings or FakeSettingsReader({}),
        session_factory=AsyncMock(),
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )
    return await svc.probe_session_direct(capabilities, timeout_sec, target=target)


async def _check_due_devices(
    db: AsyncSession, *, settings: FakeSettingsReader | None = None, deadline: float | None = None
) -> None:
    _svc._settings = settings or FakeSettingsReader({})
    await db.commit()
    engine = db.bind
    _svc._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await _svc.check_due_devices(deadline=deadline)


async def _run_scheduled_probe_series(
    db: AsyncSession,
    device: Device,
    *,
    settings: FakeSettingsReader | None = None,
    deadline: float | None = None,
) -> dict[str, Any] | None:
    _svc._settings = settings or FakeSettingsReader({})
    await db.commit()
    engine = db.bind
    _svc._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return await _svc.run_scheduled_probe_series(device.id, deadline=deadline)


async def test_session_viability_state_is_not_persisted_in_device_config(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-config-001",
        connection_target="probe-config-001",
        name="Config Cleanup Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_config={"session_viability": {"status": "failed"}},
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4729,
        desired_state=AppiumDesiredState.running,
        desired_port=4729,
        pid=12345,
        active_connection_target="127.0.0.1:4729",
    )
    db_session.add(node)
    await db_session.commit()

    loaded_device = await db_session.get(Device, device.id)
    assert loaded_device is not None
    loaded_node = await db_session.get(AppiumNode, node.id)
    assert loaded_node is not None
    loaded_device.appium_node = loaded_node

    with patch.object(
        SessionViabilityService,
        "probe_session_direct",
        new_callable=AsyncMock,
        return_value=(False, "Session create request failed: unreachable"),
    ):
        result = await run_session_viability_probe(db_session, loaded_device, checked_by="manual")

    assert result["status"] == "failed"
    await db_session.refresh(loaded_device)
    assert "session_viability" not in (loaded_device.device_config or {})


async def test_run_session_viability_probe_records_success(db_session: AsyncSession, db_host: Host) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-001",
        connection_target="probe-001",
        name="Probe Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=0,
        active_connection_target="",
    )
    db_session.add(node)
    await db_session.commit()

    loaded_device = await db_session.get(Device, device.id)
    assert loaded_device is not None
    loaded_node = await db_session.get(AppiumNode, node.id)
    assert loaded_node is not None
    loaded_device.appium_node = loaded_node

    with (
        patch(
            "app.devices.services.capability.DeviceCapabilityService.get_device_capabilities",
            new_callable=AsyncMock,
            return_value={"platformName": "Android"},
        ),
        patch.object(
            SessionViabilityService,
            "probe_session_direct",
            new_callable=AsyncMock,
            return_value=(True, None),
        ) as probe_mock,
    ):
        result = await run_session_viability_probe(db_session, loaded_device, checked_by="manual")

    assert result["status"] == "passed"
    assert result["error"] is None
    assert result["checked_by"] == "manual"
    await db_session.refresh(loaded_device)
    persisted = await get_session_viability(db_session, loaded_device)
    assert persisted is not None
    assert persisted["status"] == "passed"
    assert persisted["last_succeeded_at"] == persisted["last_attempted_at"]
    assert loaded_device.operational_state_last_emitted is DeviceOperationalState.available
    probe_mock.assert_awaited_once()
    probe_capabilities = probe_mock.await_args.args[0]
    assert probe_capabilities["platformName"] == "Android"
    assert probe_capabilities["gridfleet:probeSession"] is True
    assert probe_capabilities["gridfleet:testName"] == session_viability.PROBE_TEST_NAME


async def test_recovery_session_viability_probe_allows_offline_device(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-recovery-001",
        connection_target="probe-recovery-001",
        name="Recovery Probe Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4733,
        desired_state=AppiumDesiredState.running,
        desired_port=4733,
        pid=0,
        active_connection_target="",
    )
    db_session.add(node)
    await db_session.commit()

    loaded_device = await db_session.get(Device, device.id)
    assert loaded_device is not None
    loaded_node = await db_session.get(AppiumNode, node.id)
    assert loaded_node is not None
    loaded_device.appium_node = loaded_node

    with (
        patch(
            "app.devices.services.capability.DeviceCapabilityService.get_device_capabilities",
            new_callable=AsyncMock,
            return_value={"platformName": "Android"},
        ),
        patch.object(
            SessionViabilityService,
            "probe_session_direct",
            new_callable=AsyncMock,
            return_value=(True, None),
        ),
    ):
        result = await run_session_viability_probe(db_session, loaded_device, checked_by="recovery")

    assert result["status"] == "passed"
    await db_session.refresh(loaded_device)
    # A passed recovery probe on a healthy node derives (and emits) available:
    # the post-probe reconcile advances the ledger, matching pre-projection behavior.
    assert loaded_device.operational_state_last_emitted is DeviceOperationalState.available


async def test_run_session_viability_probe_uses_running_avd_active_target(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="manager_generated",
        identity_scope="host",
        identity_value="avd:Pixel_6_API_35",
        connection_target="Pixel_6_API_35",
        name="Pixel 6 AVD",
        os_version="15",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.emulator,
        connection_type=ConnectionType.usb,
        verified_at=datetime.now(UTC),
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4723,
        active_connection_target="emulator-5554",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=0,
    )
    db_session.add(node)
    await db_session.commit()

    loaded_device = await db_session.get(Device, device.id)
    assert loaded_device is not None
    loaded_node = await db_session.get(AppiumNode, node.id)
    assert loaded_node is not None
    loaded_device.appium_node = loaded_node
    loaded_device.host = db_host

    with patch.object(
        SessionViabilityService,
        "probe_session_direct",
        new_callable=AsyncMock,
        return_value=(True, None),
    ) as probe_mock:
        result = await run_session_viability_probe(db_session, loaded_device, checked_by="manual")

    assert result["status"] == "passed"
    assert probe_mock.await_args is not None
    capabilities = probe_mock.await_args.args[0]
    assert capabilities["appium:udid"] == "emulator-5554"
    assert capabilities["gridfleet:deviceId"] == str(device.id)
    assert capabilities["gridfleet:probeSession"] is True
    assert probe_mock.await_args.kwargs["target"] == f"http://{db_host.ip}:{loaded_node.port}"


async def test_run_session_viability_probe_writes_probe_row_on_ack(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-row-ack",
        connection_target="probe-row-ack",
        name="Probe Row Ack",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        verified_at=datetime.now(UTC),
    )
    node = AppiumNode(
        device_id=None,
        port=4723,
        active_connection_target="probe-row-ack",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=1234,
    )
    device.appium_node = node
    db_session.add_all([device, node])
    await db_session.commit()

    with patch.object(
        SessionViabilityService,
        "probe_session_direct",
        new_callable=AsyncMock,
        return_value=(True, None),
    ):
        await run_session_viability_probe(
            db_session,
            device,
            checked_by=session_viability.SessionViabilityCheckedBy.scheduled,
        )

    rows = (
        (
            await db_session.execute(
                select(Session).where(Session.device_id == device.id, Session.test_name == PROBE_TEST_NAME)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.status is SessionStatus.passed
    assert row.session_id.startswith("probe-")
    assert row.requested_capabilities is not None
    assert row.requested_capabilities[PROBE_CHECKED_BY_CAP_KEY] == "scheduled"


async def test_run_session_viability_probe_writes_probe_row_on_refusal(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-row-refuse",
        connection_target="probe-row-refuse",
        name="Probe Row Refuse",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        verified_at=datetime.now(UTC),
    )
    node = AppiumNode(
        device_id=None,
        port=4723,
        active_connection_target="probe-row-refuse",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=1234,
    )
    device.appium_node = node
    db_session.add_all([device, node])
    await db_session.commit()

    with patch.object(
        SessionViabilityService,
        "probe_session_direct",
        new_callable=AsyncMock,
        return_value=(False, "Session probe failed"),
    ):
        await run_session_viability_probe(
            db_session,
            device,
            checked_by=session_viability.SessionViabilityCheckedBy.manual,
        )

    row = (
        await db_session.execute(
            select(Session).where(Session.device_id == device.id, Session.test_name == PROBE_TEST_NAME)
        )
    ).scalar_one()
    assert row.status is SessionStatus.failed
    assert row.error_type == "probe_refused"
    assert row.error_message == "Session probe failed"
    assert row.requested_capabilities is not None
    assert row.requested_capabilities[PROBE_CHECKED_BY_CAP_KEY] == "manual"


async def test_run_session_viability_probe_rejects_non_available_device(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-002",
        connection_target="probe-002",
        name="Busy Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.busy,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    try:
        await run_session_viability_probe(db_session, device, checked_by="manual")
    except ValueError as exc:
        assert "available devices" in str(exc)
    else:
        raise AssertionError("Expected run_session_viability_probe to reject busy devices")


async def test_check_due_devices_respects_interval(db_session: AsyncSession, db_host: Host) -> None:
    settings_service._cache["general.session_viability_interval_sec"] = 86400

    due = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-003",
        connection_target="probe-003",
        name="Due Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    recent = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-004",
        connection_target="probe-004",
        name="Recent Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add_all([due, recent])
    await db_session.commit()
    await set_session_viability_control_plane_entry(
        db_session,
        str(recent.id),
        {
            "status": "passed",
            "last_attempted_at": "2099-01-01T00:00:00+00:00",
            "last_succeeded_at": "2099-01-01T00:00:00+00:00",
            "error": None,
            "checked_by": "scheduled",
        },
    )

    with patch.object(
        SessionViabilityService,
        "run_session_viability_probe",
        new_callable=AsyncMock,
        return_value={"status": "passed", "consecutive_failures": 0},
    ) as mock_probe:
        await _check_due_devices(db_session)

    assert mock_probe.await_count == 1
    assert mock_probe.await_args is not None
    assert mock_probe.await_args.kwargs["checked_by"] == "scheduled"
    assert mock_probe.await_args.args[0] == due.id
    control_plane_state = await get_session_viability_control_plane_state(db_session)
    assert str(recent.id) in control_plane_state["state"]


@pytest.mark.db
@pytest.mark.asyncio
async def test_check_due_devices_excludes_reserved_device(db_session: AsyncSession, db_host: Host) -> None:
    """A device with hold=NULL but an active reservation must not be probed."""
    settings_service._cache["general.session_viability_interval_sec"] = 86400

    reserved = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-reserved",
        connection_target="probe-reserved",
        name="Reserved Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(reserved)
    await db_session.commit()
    await create_reservation(db_session, device_id=reserved.id)
    await db_session.commit()

    with patch.object(SessionViabilityService, "run_session_viability_probe", new_callable=AsyncMock) as mock_probe:
        await _check_due_devices(db_session)

    assert mock_probe.await_count == 0


async def test_check_due_devices_runs_the_series_per_due_device(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device, node = _make_viability_device(db_host, "pass-series")
    db_session.add_all([device, node])
    await db_session.commit()

    series = AsyncMock(return_value={"status": "passed", "consecutive_failures": 0})
    monkeypatch.setattr(_svc, "run_scheduled_probe_series", series)
    await _check_due_devices(db_session)

    series.assert_awaited_once_with(device.id, deadline=ANY)


async def test_check_due_devices_defers_series_past_the_pass_budget(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pass stops starting new series once the budget elapses; deferred
    devices keep their stale last_attempted_at and stay due for the next pass.
    The admitted series is handed the same deadline, so it stops retrying at the
    budget line too. The stub series stamps the device it probed, so the stale
    stamp genuinely distinguishes the deferred devices from the probed one."""
    # Three due devices, not two: the break fires at index 1, so the pass
    # defers two of them. A deferral count of 2 is what distinguishes the
    # real ``len(due_ids) - index`` arithmetic from a flat ``inc(1)``.
    d1, n1 = _make_viability_device(db_host, "budget-1")
    d2, n2 = _make_viability_device(db_host, "budget-2")
    n2.port = 4798
    n2.desired_port = 4798
    d3, n3 = _make_viability_device(db_host, "budget-3")
    n3.port = 4797
    n3.desired_port = 4797
    db_session.add_all([d1, n1, d2, n2, d3, n3])
    await db_session.commit()
    stale = {
        "status": "passed",
        "last_attempted_at": "2020-01-01T00:00:00+00:00",
        "last_succeeded_at": "2020-01-01T00:00:00+00:00",
        "error": None,
        "checked_by": "scheduled",
        "consecutive_failures": 0,
    }
    fresh_stamp = "2021-01-01T00:00:00+00:00"
    for device in (d1, d2, d3):
        await set_session_viability_control_plane_entry(db_session, str(device.id), dict(stale))

    # Monotonic reads: deadline computation, device-1 gate (inside budget),
    # device-2 gate (past budget -> defer). Exhaustion keeps returning the
    # past-budget value, so an extra monotonic call in the implementation fails
    # a deferral assertion loudly instead of raising StopIteration. The stub
    # stays module-local (session_viability.time is the stdlib module; patching
    # its attribute would be process-wide, and the event loop reads it too).
    reads = iter([0.0, 0.0])
    monkeypatch.setattr(
        session_viability,
        "time",
        SimpleNamespace(monotonic=lambda: next(reads, 10_000.0)),
    )

    async def _stamping_series(device_id: uuid.UUID, *, deadline: float | None = None) -> dict[str, Any]:
        await set_session_viability_control_plane_entry(
            db_session, str(device_id), {**stale, "last_attempted_at": fresh_stamp}
        )
        return {"status": "passed", "consecutive_failures": 0}

    series = AsyncMock(side_effect=_stamping_series)
    monkeypatch.setattr(_svc, "run_scheduled_probe_series", series)
    deferred_before = _counter_total("gridfleet_session_viability_deferred_total")
    await _check_due_devices(db_session)
    deferred_after = _counter_total("gridfleet_session_viability_deferred_total")

    assert series.await_count == 1
    # Three devices are due and the break fires at index 1, so the counter
    # moves by the number of devices actually skipped: len(due_ids) - index
    # == 2. This is the metric F4's "does the delay matter in practice"
    # question reads, so a flat inc(1) — which would also satisfy a
    # two-device fixture — fails here.
    assert deferred_after - deferred_before == 2.0
    assert series.await_args is not None
    assert series.await_args.kwargs["deadline"] == session_viability.SCHEDULED_PASS_BUDGET_SEC

    # The probed device was stamped; the deferred one was not. The stale stamp
    # survives only on the deferred device, so the next pass finds exactly it
    # still due.
    # The due-set query has no ORDER BY, so which device is probed is not
    # fixed — whichever it was, the other two must be untouched and still due.
    remaining = {device.id: device for device in (d1, d2, d3)}
    probed = remaining.pop(series.await_args.args[0])
    probed_state = await get_session_viability(db_session, probed)
    assert probed_state is not None and probed_state["last_attempted_at"] == fresh_stamp
    for deferred in remaining.values():
        deferred_state = await get_session_viability(db_session, deferred)
        assert deferred_state is not None and deferred_state["last_attempted_at"] == stale["last_attempted_at"]
        assert await _should_run_scheduled_probe(db_session, deferred, 3600) is True


async def test_check_due_devices_uses_the_callers_deadline_verbatim(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owning sweep anchors the budget at tick start and passes the
    deadline in; the pass must hand exactly that line to the series rather
    than re-deriving its own, later one."""
    device, node = _make_viability_device(db_host, "deadline-verbatim")
    db_session.add_all([device, node])
    await db_session.commit()

    series = AsyncMock(return_value={"status": "passed", "consecutive_failures": 0})
    monkeypatch.setattr(_svc, "run_scheduled_probe_series", series)
    deadline = time.monotonic() + 1000.0
    await _check_due_devices(db_session, deadline=deadline)

    series.assert_awaited_once_with(device.id, deadline=deadline)


async def test_check_due_devices_continues_after_a_series_raises(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device whose series raises something the series does not catch (a
    deleted row raises NoResultFound out of lock_device_handle) must not skip
    the remaining due devices — otherwise one deterministic fault starves every
    device ordered after it, pass after pass."""
    d1, n1 = _make_viability_device(db_host, "guard-1")
    d2, n2 = _make_viability_device(db_host, "guard-2")
    n2.port = 4798
    n2.desired_port = 4798
    db_session.add_all([d1, n1, d2, n2])
    await db_session.commit()

    series = AsyncMock(side_effect=[NoResultFound(), {"status": "passed", "consecutive_failures": 0}])
    monkeypatch.setattr(_svc, "run_scheduled_probe_series", series)
    with patch.object(session_viability.logger, "warning") as log_spy:
        await _check_due_devices(db_session)

    assert series.await_count == 2
    assert {call.args[0] for call in series.await_args_list} == {d1.id, d2.id}
    log_spy.assert_called_once()
    assert log_spy.call_args.args[1] == series.await_args_list[0].args[0]
    assert log_spy.call_args.kwargs["exc_info"] is True


async def test_probe_session_direct_passes_through_transport_error_as_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_viability.appium_direct,
        "create_session",
        AsyncMock(return_value=(None, "ConnectError while calling http://node:4723/session", True)),
    )

    ok, error = await probe_session_direct({"platformName": "iOS"}, timeout_sec=5, target="http://node:4723")

    assert ok is False
    assert error == "Session create request failed: ConnectError while calling http://node:4723/session"


async def test_probe_session_direct_none_target_is_indeterminate() -> None:
    ok, error = await probe_session_direct({"platformName": "iOS"}, timeout_sec=5, target=None)

    assert ok is False
    assert error is not None and error.startswith("Session create request failed:")


async def test_probe_session_direct_creates_and_terminates_against_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_mock = AsyncMock(return_value=("session-1", None, False))
    terminate_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(session_viability.appium_direct, "create_session", create_mock)
    monkeypatch.setattr(session_viability.appium_direct, "terminate_session", terminate_mock)

    ok, error = await probe_session_direct(
        {"platformName": "iOS"},
        timeout_sec=5,
        target="http://node:4723/",
    )

    assert ok is True
    assert error is None
    create_mock.assert_awaited_once_with(
        "http://node:4723",
        {"capabilities": {"alwaysMatch": {"platformName": "iOS"}, "firstMatch": [{}]}},
        timeout=5,
    )
    terminate_mock.assert_awaited_once_with("http://node:4723", "session-1", timeout=5)


async def test_probe_terminate_timeout_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The create timeout can legally reach 240 s (both feeding settings raised
    to their 600 s max). Terminate must not inherit it: uncapped, one attempt
    costs create + 2 x terminate = 3 x 240 = 720 s — past the ~640 s the
    scheduler stall watchdog allows the whole appium_sweep cycle."""
    create_mock = AsyncMock(return_value=("session-1", None, False))
    terminate_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(session_viability.appium_direct, "create_session", create_mock)
    monkeypatch.setattr(session_viability.appium_direct, "terminate_session", terminate_mock)

    ok, error = await probe_session_direct({"platformName": "iOS"}, timeout_sec=240, target="http://node:4723")

    assert ok is True
    assert error is None
    assert create_mock.await_args is not None
    assert create_mock.await_args.kwargs["timeout"] == 240
    terminate_mock.assert_awaited_once_with(
        "http://node:4723", "session-1", timeout=session_viability._PROBE_TERMINATE_TIMEOUT_CAP_SEC
    )


def test_grid_probe_response_to_result_maps_all_shapes() -> None:
    assert grid_probe_response_to_result((True, None)).status == "ack"
    assert grid_probe_response_to_result((False, None)).status == "refused"

    refused = grid_probe_response_to_result((False, "device offline"))
    assert refused.status == "refused"
    assert refused.detail == "device offline"

    indeterminate = grid_probe_response_to_result((False, "Session create request failed: ConnectError"))
    assert indeterminate.status == "indeterminate"
    assert indeterminate.detail == "Session create request failed: ConnectError"


def test_session_viability_small_helpers_cover_error_shapes() -> None:
    assert _parse_timestamp(None) is None
    assert _parse_timestamp("") is None
    assert _parse_timestamp("not-a-date") is None
    assert _parse_timestamp("2026-01-02T03:04:05Z") is not None


async def test_record_session_viability_result_preserves_previous_success_and_clears_config(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-record-001",
        connection_target="probe-record-001",
        name="Probe Record Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_config={"session_viability": {"status": "legacy"}},
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    passed = await _svc.record_session_viability_result(db_session, device, status="passed", checked_by="manual")
    failed = await _svc.record_session_viability_result(
        db_session,
        device,
        status="failed",
        error="probe failed",
        checked_by="scheduled",
    )

    assert passed["last_succeeded_at"] is not None
    assert failed["status"] == "failed"
    assert failed["last_succeeded_at"] == passed["last_succeeded_at"]
    assert "session_viability" not in (device.device_config or {})


async def test_should_run_scheduled_probe_covers_skip_and_due_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-schedule-001",
        connection_target="probe-schedule-001",
        name="Probe Schedule Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    assert await _should_run_scheduled_probe(db_session, device, 0) is False
    busy_session = Session(session_id="probe-schedule-busy", device_id=device.id, status=SessionStatus.running)
    db_session.add(busy_session)
    await db_session.flush()
    assert await _should_run_scheduled_probe(db_session, device, 60) is False
    await db_session.delete(busy_session)
    await db_session.flush()

    monkeypatch.setattr("app.sessions.service_viability.is_ready_for_use_async", AsyncMock(return_value=False))
    assert await _should_run_scheduled_probe(db_session, device, 60) is False

    monkeypatch.setattr("app.sessions.service_viability.is_ready_for_use_async", AsyncMock(return_value=True))
    await set_session_viability_control_plane_entry(
        db_session,
        str(device.id),
        {
            "status": "passed",
            "last_attempted_at": "not-a-date",
            "last_succeeded_at": None,
            "error": None,
            "checked_by": "scheduled",
        },
    )
    assert await _should_run_scheduled_probe(db_session, device, 60) is True

    probe_row = Session(
        session_id=f"probe-{uuid.uuid4()}",
        device_id=device.id,
        test_name=PROBE_TEST_NAME,
        status=SessionStatus.pending,
    )
    db_session.add(probe_row)
    await db_session.flush()
    assert await _should_run_scheduled_probe(db_session, device, 60) is False


@pytest.mark.parametrize(
    ("create_return", "expected_error"),
    [
        # HTTP refusal: the node answered, session refused — surface the raw message.
        ((None, "bad caps", False), "bad caps"),
        # Non-JSON refusal body falls back to raw text in appium_direct.
        ((None, "plain body", False), "plain body"),
        # Empty error string still produces a deterministic refusal message.
        ((None, "", False), "Session create failed"),
    ],
)
async def test_probe_session_direct_create_failure_paths(
    create_return: tuple[None, str, bool], expected_error: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_viability.appium_direct, "create_session", AsyncMock(return_value=create_return))

    ok, error = await probe_session_direct({"platformName": "Android"}, timeout_sec=3, target="http://node:4723")

    assert ok is False
    assert error == expected_error


async def test_probe_session_direct_cleanup_failure_is_indeterminate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        session_viability.appium_direct, "create_session", AsyncMock(return_value=("session-1", None, False))
    )
    monkeypatch.setattr(session_viability.appium_direct, "terminate_session", AsyncMock(return_value=False))

    ok, error = await probe_session_direct({"platformName": "Android"}, timeout_sec=3, target="http://node:4723")

    assert ok is False
    assert error == "Session created but cleanup failed"


async def test_probe_session_direct_terminates_created_session_when_on_created_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raised ``on_created`` must NOT leak the created Appium session.

    Regression: the probe creates a session (which sets up the driver's
    forwarded ports, e.g. Android systemPort). If ``on_created`` raises before
    the terminate, the session used to be left alive — its forwarded ports
    orphaned on the host — and the leaked port then fails the *next* session
    create (uia2/WDA busy-check), so the device can never recover. Cleanup must
    be guaranteed even on the raising path.
    """
    create_mock = AsyncMock(return_value=("session-1", None, False))
    terminate_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(session_viability.appium_direct, "create_session", create_mock)
    monkeypatch.setattr(session_viability.appium_direct, "terminate_session", terminate_mock)

    async def _on_created(_session_id: str) -> None:
        raise RuntimeError("promotion-commit-failed")

    svc = SessionViabilityService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        session_factory=AsyncMock(),
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="promotion-commit-failed"):
        await svc.probe_session_direct(
            {"platformName": "Android"},
            timeout_sec=3,
            target="http://node:4723",
            on_created=_on_created,
        )

    terminate_mock.assert_awaited_once_with("http://node:4723", "session-1", timeout=3)


async def test_probe_session_direct_retries_terminate_on_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single transient terminate failure must not leak the session.

    Regression: cleanup was a one-shot DELETE — one transient blip left the
    session (and its forwarded ports) alive and the probe reported "cleanup
    failed", stopping the device. Retry the terminate before giving up.
    """
    create_mock = AsyncMock(return_value=("session-1", None, False))
    terminate_mock = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(session_viability.appium_direct, "create_session", create_mock)
    monkeypatch.setattr(session_viability.appium_direct, "terminate_session", terminate_mock)

    ok, error = await probe_session_direct({"platformName": "Android"}, timeout_sec=3, target="http://node:4723")

    assert ok is True
    assert error is None
    assert terminate_mock.await_count == 2


async def test_run_session_viability_probe_rejects_missing_running_node(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-no-node",
        connection_target="probe-no-node",
        name="No Node Probe Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    loaded_device = await db_session.get(Device, device.id)
    assert loaded_device is not None
    loaded_device.appium_node = None

    result = await run_session_viability_probe(db_session, loaded_device, checked_by="manual")

    assert result["status"] == "failed"
    assert result["error"] == "Appium node is not running"


async def test_recovery_probe_skips_unobserved_node_instead_of_failing(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A recovery probe races the node coming up: the agent has been told to start
    it but the observed pid may not have folded yet. An unobserved node must be a
    benign skip (retry next tick), NOT a failure — a hard fail here commissions an
    auto-stop that kills the node recovery just started, spiraling into exponential
    backoff. (A genuinely un-startable node still trips backoff via the agent's
    start_failure report.)"""
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-recovery-unobserved",
        connection_target="Pixel_6",
        name="Recovery Unobserved Node",
        os_version="17",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.emulator,
        connection_type=ConnectionType.virtual,
    )
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(
        device_id=device.id,
        port=4728,
        desired_state=AppiumDesiredState.running,
        desired_port=4728,
        pid=None,
        active_connection_target=None,
    )
    db_session.add(node)
    await db_session.commit()

    loaded_device = await db_session.get(Device, device.id)
    assert loaded_device is not None
    loaded_node = await db_session.get(AppiumNode, node.id)
    assert loaded_node is not None
    assert not loaded_node.observed_running
    loaded_device.appium_node = loaded_node

    with pytest.raises(SessionViabilityProbeNotPermittedError):
        await run_session_viability_probe(db_session, loaded_device, checked_by="recovery")

    # No failed viability state was written, so nothing escalates to an auto-stop.
    assert await get_session_viability(db_session, loaded_device) is None


async def test_run_session_viability_probe_rejects_duplicate_and_not_ready(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-guard-001",
        connection_target="probe-guard-001",
        name="Probe Guard Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=1234,
        active_connection_target="probe-target",
    )
    device.appium_node = node
    db_session.add(node)
    await db_session.commit()

    row = Session(
        session_id=f"probe-{uuid.uuid4()}",
        device_id=device.id,
        test_name=PROBE_TEST_NAME,
        status=SessionStatus.pending,
    )
    db_session.add(row)
    await db_session.commit()
    with pytest.raises(ValueError, match="already in progress"):
        await run_session_viability_probe(db_session, device, checked_by="manual")

    row.status = SessionStatus.passed
    row.ended_at = datetime.now(UTC)
    await db_session.commit()
    monkeypatch.setattr("app.sessions.service_viability.is_ready_for_use_async", AsyncMock(return_value=False))
    monkeypatch.setattr(
        "app.sessions.service_viability.readiness_error_detail_async",
        AsyncMock(return_value="not ready enough"),
    )

    with pytest.raises(ValueError, match="not ready enough"):
        await run_session_viability_probe(db_session, device, checked_by="manual")


async def test_run_session_viability_probe_changed_state_and_health_handler_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-handler-001",
        connection_target="probe-handler-001",
        name="Probe Handler Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_config={"session_viability": {"status": "failed"}},
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(
        device_id=device.id,
        port=4780,
        desired_state=AppiumDesiredState.running,
        desired_port=4780,
        pid=1234,
        active_connection_target="probe-handler-001",
    )
    db_session.add(node)
    await db_session.commit()

    monkeypatch.setattr(
        DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={"platformName": "Android"})
    )
    monkeypatch.setattr(_svc, "probe_session_direct", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(
        session_viability,
        "_write_session_viability",
        AsyncMock(return_value={"status": "failed", "consecutive_failures": 1}),
    )
    handler = AsyncMock()
    _svc.configure_health_failure_handler(handler)
    try:
        state = await run_session_viability_probe(
            db_session,
            device,
            checked_by=session_viability.SessionViabilityCheckedBy.manual,
            settings=FakeSettingsReader(
                {
                    "general.session_viability_failure_threshold": 1,
                    "general.session_viability_timeout_sec": 5,
                }
            ),
        )
    finally:
        _svc.configure_health_failure_handler(None)

    assert state == {"status": "failed", "consecutive_failures": 1}
    await db_session.refresh(device)
    assert device.device_config == {}
    handler.assert_awaited_once()
    assert handler.await_args.kwargs["reason"] == "Appium session viability probe failed"


async def _capture_probe_create_timeout(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: FakeSettingsReader,
    identity: str,
    port: int,
) -> int:
    """Run a viability probe on an available device and return the timeout the probe
    passed to ``probe_session_direct`` (the Appium-create timeout)."""
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=identity,
        connection_target=identity,
        name=f"Probe Timeout {identity}",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    node = AppiumNode(
        device_id=device.id,
        port=port,
        desired_state=AppiumDesiredState.running,
        desired_port=port,
        pid=1234,
        active_connection_target=identity,
    )
    device.appium_node = node
    db_session.add_all([device, node])
    await db_session.commit()

    locked = MagicMock(id=device.id, operational_state=DeviceOperationalState.available, hold=None)
    monkeypatch.setattr(session_viability, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(session_viability.device_locking, "lock_device", AsyncMock(return_value=locked))
    monkeypatch.setattr(
        session_viability,
        "IntentService",
        MagicMock(
            return_value=MagicMock(reconcile_now=AsyncMock(), mark_dirty=AsyncMock(), revoke_intents=AsyncMock())
        ),
    )
    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={}))
    monkeypatch.setattr(session_viability, "_write_session_viability", AsyncMock(return_value={"status": "passed"}))
    probe_spy = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(_svc, "probe_session_direct", probe_spy)

    await run_session_viability_probe(
        db_session,
        device,
        checked_by=session_viability.SessionViabilityCheckedBy.manual,
        settings=settings,
    )

    probe_spy.assert_awaited_once()
    # probe_session_direct(capabilities, timeout_sec, *, target, on_created)
    return int(probe_spy.await_args.args[1])


async def test_probe_create_timeout_is_bounded_below_claim_window(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe whose configured timeout meets/exceeds ``grid.claim_window_sec`` must
    still be capped below the claim window, so the allocation reaper cannot fail the
    probe's in-flight ``pending`` birth-row mid-create (WS-16.1 flap: a slow create
    ran the full 120s ``session_viability_timeout_sec``, colliding with the equal
    120s claim window and flapping the device available->offline)."""
    claim_window = 120
    passed_timeout = await _capture_probe_create_timeout(
        db_session,
        db_host,
        monkeypatch,
        settings=FakeSettingsReader(
            {"general.session_viability_timeout_sec": 120, "grid.claim_window_sec": claim_window}
        ),
        identity="probe-timeout-cap-001",
        port=4790,
    )
    assert passed_timeout <= claim_window - CREATE_TIMEOUT_MARGIN_SEC
    assert passed_timeout == int(effective_create_timeout(claim_window))


async def test_probe_create_timeout_below_claim_window_passes_through(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured timeout already comfortably below the claim window is honored
    as-is — the cap only lowers, never raises, the probe's create timeout."""
    passed_timeout = await _capture_probe_create_timeout(
        db_session,
        db_host,
        monkeypatch,
        settings=FakeSettingsReader({"general.session_viability_timeout_sec": 30, "grid.claim_window_sec": 120}),
        identity="probe-timeout-passthrough-001",
        port=4791,
    )
    assert passed_timeout == 30


async def test_run_session_viability_probe_restores_previous_state_on_exception(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-exception-001",
        connection_target="probe-exception-001",
        name="Probe Exception Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.offline,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4781,
        desired_state=AppiumDesiredState.running,
        desired_port=4781,
        pid=1234,
        active_connection_target="probe-exception-001",
    )
    device.appium_node = node
    db_session.add_all([device, node])
    await db_session.commit()

    locked = MagicMock(id=device.id, operational_state=DeviceOperationalState.offline, hold=None)
    monkeypatch.setattr(session_viability.control_plane_state_store, "delete_value", AsyncMock())
    monkeypatch.setattr(session_viability, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(session_viability.device_locking, "lock_device", AsyncMock(return_value=locked))
    # After Task 10: no _MACHINE; exception path calls reconcile_now. Patch IntentService.
    mark_dirty = AsyncMock()
    monkeypatch.setattr(
        session_viability,
        "IntentService",
        MagicMock(
            return_value=MagicMock(
                reconcile_now=mark_dirty,
                mark_dirty=AsyncMock(),
            )
        ),
    )
    monkeypatch.setattr(
        DeviceCapabilityService,
        "get_device_capabilities",
        AsyncMock(side_effect=RuntimeError("caps")),
    )
    with pytest.raises(RuntimeError, match="caps"):
        await run_session_viability_probe(
            db_session,
            device,
            checked_by=session_viability.SessionViabilityCheckedBy.recovery,
            settings=FakeSettingsReader({"general.session_viability_timeout_sec": 5}),
        )

    # Exception paths leave the projection to the reconciler scan.
    mark_dirty.assert_not_awaited()


async def test_run_session_viability_probe_no_node_commit_and_available_exception_restore(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Sub-case 1: a real device with no Appium node. ``_prepare_probe`` records
    # a ``failed`` terminal state (no probe run) and commits via its own
    # ``begin()`` context — no outer transaction is held across any remote call.
    no_node_device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-no-node-001",
        connection_target="probe-no-node-001",
        name="No Node Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_config={"session_viability": {"status": "legacy"}},
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(no_node_device)
    await db_session.commit()

    monkeypatch.setattr(session_viability, "is_ready_for_use_async", AsyncMock(return_value=True))

    state = await run_session_viability_probe(
        db_session,
        no_node_device,
        checked_by=session_viability.SessionViabilityCheckedBy.manual,
        settings=FakeSettingsReader({"general.session_viability_timeout_sec": 5}),
    )

    assert state["status"] == "failed"
    await db_session.refresh(no_node_device)
    assert no_node_device.device_config == {}

    # Sub-case 2: a real device with a running node whose capability read raises.
    # The exception propagates out of ``_prepare_probe`` (its ``begin()`` context
    # rolls back); no reconcile runs on the exception path.
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-caps-raise-001",
        connection_target="probe-caps-raise-001",
        name="Caps Raise Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(
        device_id=device.id,
        port=4791,
        desired_state=AppiumDesiredState.running,
        desired_port=4791,
        pid=1234,
        active_connection_target="probe-caps-raise-001",
    )
    db_session.add(node)
    await db_session.commit()

    monkeypatch.setattr(
        DeviceCapabilityService,
        "get_device_capabilities",
        AsyncMock(side_effect=RuntimeError("caps")),
    )
    with pytest.raises(RuntimeError, match="caps"):
        await run_session_viability_probe(
            db_session,
            device,
            checked_by=session_viability.SessionViabilityCheckedBy.manual,
            settings=FakeSettingsReader({"general.session_viability_timeout_sec": 5}),
        )


class _CountingSessionFactory:
    """Duck-typed ``async_sessionmaker`` wrapper that counts open transactions.

    The viability probe opens its own fresh sessions via ``self._session_factory``
    for each phase (prepare/confirm/finalize/escalate). No transaction may be
    open across the remote Appium ``create_session``/``terminate_session`` calls.
    """

    def __init__(self, real: async_sessionmaker[AsyncSession]) -> None:
        self._real = real
        self.active = 0

    def __call__(self) -> _CountingSession:
        return _CountingSession(self._real(), self)

    def begin(self) -> _CountingBegin:
        return _CountingBegin(self._real.begin(), self)


class _CountingSession:
    def __init__(self, session: AsyncSession, factory: _CountingSessionFactory) -> None:
        self._session = session
        self._factory = factory

    async def __aenter__(self) -> AsyncSession:
        self._factory.active += 1
        try:
            await self._session.__aenter__()
        except BaseException:
            self._factory.active -= 1
            raise
        return self._session

    async def __aexit__(self, *args: object) -> None:
        try:
            await self._session.__aexit__(*args)
        finally:
            self._factory.active -= 1


class _CountingBegin:
    def __init__(self, cm: object, factory: _CountingSessionFactory) -> None:
        self._cm = cm
        self._factory = factory

    async def __aenter__(self) -> AsyncSession:
        self._factory.active += 1
        try:
            return await self._cm.__aenter__()  # type: ignore[attr-defined]
        except BaseException:
            self._factory.active -= 1
            raise

    async def __aexit__(self, *args: object) -> None:
        try:
            await self._cm.__aexit__(*args)  # type: ignore[attr-defined]
        finally:
            self._factory.active -= 1


async def test_probe_phases_hold_no_transaction_across_appium_io(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No DB transaction is open across the remote Appium create/terminate calls.

    Each probe phase owns and closes its own fresh session before the remote
    effect: ``create_session`` and ``terminate_session`` must observe an active
    transaction count of 0.
    """
    from app.grid import appium_direct

    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-txn-001",
        connection_target="probe-txn-001",
        name="Txn Tracker Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    node = AppiumNode(
        device_id=device.id,
        port=4771,
        desired_state=AppiumDesiredState.running,
        desired_port=4771,
        pid=1234,
        active_connection_target="probe-txn-001",
    )
    db_session.add(node)
    await db_session.commit()

    await db_session.commit()
    factory = _CountingSessionFactory(async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False))
    _svc._settings = FakeSettingsReader({"general.session_viability_timeout_sec": 5})
    _svc._session_factory = factory  # type: ignore[assignment]

    monkeypatch.setattr(
        DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={"platformName": "Android"})
    )

    create_txn_counts: list[int] = []
    terminate_txn_counts: list[int] = []

    async def _tracking_create(
        base: str, payload: dict[str, Any], *, timeout: int
    ) -> tuple[str | None, str | None, bool]:
        create_txn_counts.append(factory.active)
        return "fake-probe-session", None, False

    async def _tracking_terminate(base: str, session_id: str, *, timeout: int) -> bool:
        terminate_txn_counts.append(factory.active)
        return True

    monkeypatch.setattr(appium_direct, "create_session", _tracking_create)
    monkeypatch.setattr(appium_direct, "terminate_session", _tracking_terminate)

    # Drive the probe via the service directly (the ``run_session_viability_probe``
    # local wrapper also commits setup, which is already done here).
    result = await _svc.run_session_viability_probe(
        device.id, checked_by=session_viability.SessionViabilityCheckedBy.manual
    )

    assert result["status"] == "passed"
    assert create_txn_counts == [0], f"active transaction during create_session: {create_txn_counts}; expected 0"
    assert terminate_txn_counts == [0], (
        f"active transaction during terminate_session: {terminate_txn_counts}; expected 0"
    )


def test_classify_session_error_recognises_grid_no_slot() -> None:
    no_slot = "Could not start a new session. {value={error=session not created}} Driver info: driver.version: unknown"
    assert session_viability._classify_session_error(no_slot) == "driver_not_loaded"
    assert session_viability._classify_session_error("ADB device 'X' not found within timeout") == "driver"
    assert session_viability._classify_session_error(None) is None


async def _run_failing_probe(
    db: AsyncSession,
    device: Device,
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: str = "boom",
    threshold: int = 3,
    handler: AsyncMock | None = None,
) -> dict[str, object]:
    """Helper: drive ``run_session_viability_probe`` with a failing grid probe."""

    def _settings(key: str) -> int:
        if "failure_threshold" in key:
            return threshold
        return 5

    monkeypatch.setattr(_svc, "probe_session_direct", AsyncMock(return_value=(False, error)))
    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={}))
    if handler is not None:
        _svc.configure_health_failure_handler(handler)
    return await run_session_viability_probe(
        db,
        device,
        checked_by=session_viability.SessionViabilityCheckedBy.scheduled,
        settings=FakeSettingsReader(
            {
                "general.session_viability_failure_threshold": _settings("general.session_viability_failure_threshold"),
                "general.session_viability_timeout_sec": _settings("general.session_viability_timeout_sec"),
            }
        ),
    )


def _make_viability_device(db_host: Host, suffix: str) -> tuple[Device, AppiumNode]:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=f"viab-strike-{suffix}",
        connection_target=f"viab-strike-{suffix}",
        name=f"Viability Strike {suffix}",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4799,
        desired_state=AppiumDesiredState.running,
        desired_port=4799,
        pid=999,
        active_connection_target=f"viab-strike-{suffix}",
    )
    device.appium_node = node
    return device, node


async def test_single_viability_failure_does_not_escalate_below_threshold(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device, node = _make_viability_device(db_host, "below")
    db_session.add_all([device, node])
    await db_session.commit()

    handler = AsyncMock()
    try:
        state = await _run_failing_probe(
            db_session, device, monkeypatch, error="grid hiccup", threshold=3, handler=handler
        )
    finally:
        _svc.configure_health_failure_handler(None)

    assert state["status"] == "failed"
    assert state.get("consecutive_failures") == 1
    handler.assert_not_awaited()


async def test_viability_escalates_after_threshold_consecutive_failures(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device, node = _make_viability_device(db_host, "threshold")
    db_session.add_all([device, node])
    await db_session.commit()

    handler = AsyncMock()
    try:
        for _ in range(3):
            # Each probe leaves the device offline; the recovery branch is what
            # lets the next iteration re-enter the probe path.
            device.operational_state_last_emitted = DeviceOperationalState.available
            await _run_failing_probe(db_session, device, monkeypatch, error="grid hiccup", threshold=3, handler=handler)
    finally:
        _svc.configure_health_failure_handler(None)

    final = await get_session_viability(db_session, device)
    assert final is not None and final["status"] == "failed"
    assert final["consecutive_failures"] == 3
    handler.assert_awaited_once()


async def test_passing_probe_resets_viability_failure_counter(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device, node = _make_viability_device(db_host, "reset")
    db_session.add_all([device, node])
    await db_session.commit()

    handler = AsyncMock()

    def _settings(key: str) -> int:
        if "failure_threshold" in key:
            return 3
        return 5

    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={}))
    _svc.configure_health_failure_handler(handler)
    try:
        # Two consecutive failures get to count=2.
        monkeypatch.setattr(_svc, "probe_session_direct", AsyncMock(return_value=(False, "transient")))
        for _ in range(2):
            device.operational_state_last_emitted = DeviceOperationalState.available
            await run_session_viability_probe(
                db_session,
                device,
                checked_by=session_viability.SessionViabilityCheckedBy.scheduled,
                settings=FakeSettingsReader(
                    {
                        "general.session_viability_failure_threshold": _settings(
                            "general.session_viability_failure_threshold"
                        ),
                        "general.session_viability_timeout_sec": _settings("general.session_viability_timeout_sec"),
                    }
                ),
            )

        # A passing probe must reset the counter back to 0.
        monkeypatch.setattr(_svc, "probe_session_direct", AsyncMock(return_value=(True, None)))
        device.operational_state_last_emitted = DeviceOperationalState.available
        await run_session_viability_probe(
            db_session, device, checked_by=session_viability.SessionViabilityCheckedBy.scheduled
        )
        mid = await get_session_viability(db_session, device)
        assert mid is not None and mid["consecutive_failures"] == 0

        # One more failure must start the count over, not jump straight to threshold.
        monkeypatch.setattr(_svc, "probe_session_direct", AsyncMock(return_value=(False, "transient again")))
        device.operational_state_last_emitted = DeviceOperationalState.available
        await run_session_viability_probe(
            db_session, device, checked_by=session_viability.SessionViabilityCheckedBy.scheduled
        )
    finally:
        _svc.configure_health_failure_handler(None)

    final = await get_session_viability(db_session, device)
    assert final is not None and final["consecutive_failures"] == 1
    handler.assert_not_awaited()


async def test_write_session_viability_persists_error_category(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: ``error_category`` must survive the round-trip through
    the control-plane state store, not just live on the in-memory return value
    of ``_write_session_viability``. Without this, the ``_classify_session_error``
    classifier would silently stop being observable to operators."""
    device, node = _make_viability_device(db_host, "category")
    db_session.add_all([device, node])
    await db_session.commit()

    grid_error = (
        "Could not start a new session. {value={error=session not created}} Driver info: driver.version: unknown"
    )

    def _settings(key: str) -> int:
        if "failure_threshold" in key:
            return 5  # below threshold so no escalation interferes
        return 5

    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={}))
    monkeypatch.setattr(_svc, "probe_session_direct", AsyncMock(return_value=(False, grid_error)))

    await run_session_viability_probe(
        db_session,
        device,
        checked_by=session_viability.SessionViabilityCheckedBy.scheduled,
        settings=FakeSettingsReader(
            {
                "general.session_viability_failure_threshold": _settings("general.session_viability_failure_threshold"),
                "general.session_viability_timeout_sec": _settings("general.session_viability_timeout_sec"),
            }
        ),
    )

    persisted = await get_session_viability(db_session, device)
    assert persisted is not None
    assert persisted["status"] == "failed"
    assert persisted["error_category"] == "driver_not_loaded"

    # A passing probe must clear ``error_category`` so a recovered device does
    # not keep an old infra tag attached.
    monkeypatch.setattr(_svc, "probe_session_direct", AsyncMock(return_value=(True, None)))
    device.operational_state_last_emitted = DeviceOperationalState.available
    await run_session_viability_probe(
        db_session, device, checked_by=session_viability.SessionViabilityCheckedBy.scheduled
    )
    after_pass = await get_session_viability(db_session, device)
    assert after_pass is not None
    assert after_pass["status"] == "passed"
    assert after_pass["error_category"] is None


async def test_run_session_viability_probe_passes_does_not_flap_offline_when_stop_pending(
    db_session: AsyncSession,
    db_host: Host,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    """Regression: a passing probe must not flap the device offline when a
    stale graceful-stop intent has marked ``node.stop_pending=True``.

    A stale ``connectivity:*`` stop intent can leave
    ``node.stop_pending=True`` on a fully-healthy device. The scheduled
    viability probe then runs, passes (Grid acks), and the post-probe
    restore path used the ``ready_operational_state(...)`` projection —
    which folded ``appium_node_stop_in_flight`` into the operational
    axis and returned ``offline``. The device transitioned busy →
    offline ("Session viability probe finished"), and seconds later the
    health-recovery loop flipped it back ("Health checks recovered"),
    producing a toast pair per probe cycle.

    The probe-passed branch is an event ("probe ok"), not a projection.
    It must drive SESSION_ENDED (busy → available) directly. Real offline
    transitions for in-flight stops belong to the connectivity loop and
    node_health, which fire on their own schedules with their own
    reasons.
    """
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="probe-stop-pending-repro",
        connection_target="probe-stop-pending-repro",
        name="Probe Stop Pending Repro",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=12345,
        active_connection_target="probe-stop-pending-repro",
        stop_pending=False,
    )
    db_session.add(node)
    await db_session.commit()

    loaded_device = await db_session.get(Device, device.id)
    assert loaded_device is not None
    loaded_node = await db_session.get(AppiumNode, node.id)
    assert loaded_node is not None
    loaded_device.appium_node = loaded_node

    event_bus_capture.clear()
    with (
        patch(
            "app.devices.services.capability.DeviceCapabilityService.get_device_capabilities",
            new_callable=AsyncMock,
            return_value={"platformName": "Android"},
        ),
        patch.object(
            SessionViabilityService,
            "probe_session_direct",
            new_callable=AsyncMock,
            return_value=(True, None),
        ),
    ):
        await run_session_viability_probe(
            db_session,
            loaded_device,
            checked_by=session_viability.SessionViabilityCheckedBy.scheduled,
        )

    await dispatch_committed_events()
    op_events = [
        payload
        for name, payload in event_bus_capture
        if name == "device.operational_state_changed" and payload["device_id"] == str(loaded_device.id)
    ]
    spurious_offline = [p for p in op_events if p["new_operational_state"] == "offline"]
    assert spurious_offline == [], (
        "passing probe must not project transient stop_pending into operational_state; "
        f"got spurious offline event(s) {spurious_offline}"
    )
    await db_session.refresh(loaded_device)
    assert loaded_device.operational_state_last_emitted == DeviceOperationalState.available


def test_probe_always_match_routes_on_device_id_not_udid() -> None:
    """Probes must pin on the stable gridfleet:deviceId, never appium:udid.

    The slot stereotype no longer advertises appium:udid (it is a driver
    connection detail, not a routing key), so sending it in alwaysMatch would
    make Selenium's DefaultSlotMatcher reject the slot.
    """
    full_caps = {
        "platformName": "Android",
        "appium:automationName": "UiAutomator2",
        "appium:udid": "emulator-5554",
        "appium:deviceName": "Pixel",
        "gridfleet:deviceId": "abc-123",
        "gridfleet:probeSession": True,
        "gridfleet:testName": "gridfleet-probe",
    }

    filtered = _filter_probe_always_match(full_caps)

    assert "appium:udid" not in filtered
    assert "appium:deviceName" not in filtered
    assert filtered["gridfleet:deviceId"] == "abc-123"
    assert filtered["platformName"] == "Android"
    assert filtered["gridfleet:probeSession"] is True
    assert "appium:udid" not in _PROBE_ALWAYS_MATCH_KEYS
    assert "appium:deviceName" not in _PROBE_ALWAYS_MATCH_KEYS


# --------------------------------------------------------------------------- #
# The escalation command's own transaction boundary                            #
# --------------------------------------------------------------------------- #


def _wired_escalation_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> SessionViabilityService:
    """A viability service wired to the real handler, as ``composition.py`` wires it.

    ``configure_health_failure_handler(lifecycle_policy_svc.handle_health_failure)``
    is the production wiring (``app/composition.py:180``). Every other test in this
    module installs an ``AsyncMock`` there, which cannot say anything about whose
    transaction the handler's writes land in — so this builds the real chain.
    """
    incidents = LifecycleIncidentService()
    policy = LifecyclePolicyService(
        publisher=_test_event_bus,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=_test_event_bus,
            reservation=RunReservationService(),
            incidents=incidents,
        ),
        incidents=incidents,
        viability=AsyncMock(),
        node_manager=AsyncMock(),
    )
    service = SessionViabilityService(
        publisher=_test_event_bus,
        settings=FakeSettingsReader({"general.session_viability_failure_threshold": 1}),
        session_factory=session_factory,
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )
    service.configure_health_failure_handler(policy.handle_health_failure)
    return service


async def _seed_escalation_device(db_session: AsyncSession, db_host: Host, identity: str) -> uuid.UUID:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=identity,
        connection_target=identity,
        name=identity,
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()
    return device.id


async def test_escalation_command_publishes_the_wired_handlers_writes(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """``_escalate_probe_failure_command`` owns the boundary for the wired handler.

    The positive half: the handler's escalation does reach the database through
    the command's ``begin()``, asserted from a session that never saw it staged.
    The falsifying half — that the *command's* transaction, and nothing inside the
    handler, is what published it — is the test below.
    """
    device_id = await _seed_escalation_device(db_session, db_host, "viab-escalation-boundary")
    service = _wired_escalation_service(db_session_maker)

    await service._escalate_probe_failure_command(
        device_id,
        {"consecutive_failures": 1},
        result=(False, "Appium session viability probe failed"),
        checked_by=session_viability.SessionViabilityCheckedBy.scheduled,
    )

    async with db_session_maker() as verify:
        entries = (
            (
                await verify.execute(
                    select(DeviceRemediationLogEntry).where(DeviceRemediationLogEntry.device_id == device_id)
                )
            )
            .scalars()
            .all()
        )
    sources = {entry.source for entry in entries}
    assert sources == {"session_viability"}, f"the handler's escalation did not reach the database: {entries}"
    assert any(entry.kind == "failure" for entry in entries)


async def test_escalation_command_rollback_takes_the_wired_handlers_writes_with_it(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """The mirror of the ``handle_session_finished`` caller-boundary test, on the real path.

    ``handle_health_failure`` used to commit internally, so this command opened a
    plain session on purpose to avoid a double commit — and a failure after the
    handler returned could not undo what the handler had already published. The
    failure injected here is a real statement error on the command's own
    transaction, not a raise from a patched method, so the transaction is genuinely
    aborted the way production would abort it.
    """
    device_id = await _seed_escalation_device(db_session, db_host, "viab-escalation-rollback")
    service = _wired_escalation_service(db_session_maker)
    real_escalate = service._escalate_probe_failure

    async def escalate_then_fail(
        db: AsyncSession,
        device: Device,
        state: dict[str, Any],
        *,
        result: tuple[bool, str | None],
        checked_by: session_viability.SessionViabilityCheckedBy,
    ) -> None:
        await real_escalate(db, device, state, result=result, checked_by=checked_by)
        await db.execute(text("SELECT 1 / 0"))

    service._escalate_probe_failure = escalate_then_fail  # type: ignore[method-assign]

    with pytest.raises(SQLAlchemyError):
        await service._escalate_probe_failure_command(
            device_id,
            {"consecutive_failures": 1},
            result=(False, "Appium session viability probe failed"),
            checked_by=session_viability.SessionViabilityCheckedBy.scheduled,
        )

    async with db_session_maker() as verify:
        entries = (
            (
                await verify.execute(
                    select(DeviceRemediationLogEntry).where(DeviceRemediationLogEntry.device_id == device_id)
                )
            )
            .scalars()
            .all()
        )
    assert entries == [], "the handler committed behind the escalation command's boundary"


async def test_escalation_command_persists_the_failure_entry_for_a_maintenance_held_device(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    """The maintenance-suppressed branch now records its failure entry.

    ``handle_health_failure`` appends the ladder failure entry *before* it checks
    ``in_maintenance`` and returns ``"suppressed"`` (``policy.py:313-314``), and
    that early return never committed. On the old plain session the entry was
    therefore dropped on close; on the command's ``begin()`` it lands. This is a
    deliberate consequence of the conversion — a maintenance-held device that
    keeps failing viability probes now accumulates ladder failure history — and
    this test is what states the choice rather than leaving it implicit.
    """
    device_id = await _seed_escalation_device(db_session, db_host, "viab-escalation-maintenance")
    device = await db_session.get(Device, device_id)
    assert device is not None
    set_maintenance_reason(device, "operator hold")
    await db_session.commit()

    service = _wired_escalation_service(db_session_maker)
    await service._escalate_probe_failure_command(
        device_id,
        {"consecutive_failures": 1},
        result=(False, "Appium session viability probe failed"),
        checked_by=session_viability.SessionViabilityCheckedBy.scheduled,
    )

    async with db_session_maker() as verify:
        entries = (
            (
                await verify.execute(
                    select(DeviceRemediationLogEntry).where(DeviceRemediationLogEntry.device_id == device_id)
                )
            )
            .scalars()
            .all()
        )
        held = await verify.get(Device, device_id)
    assert [(entry.kind, entry.source) for entry in entries] == [("failure", "session_viability")]
    # Suppressed means suppressed: the maintenance hold still stands and no
    # auto-stop ran alongside the recorded failure.
    assert held is not None
    assert ps(held).get("maintenance_reason") == "operator hold"


async def test_scheduled_retry_probes_device_offline_solely_from_viability(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry flag admits a device whose only defect is its own failed
    viability column — the state the series' previous attempt created."""
    device, node = _make_viability_device(db_host, "retry-ok")
    device.session_viability_status = "failed"
    device.session_viability_error = "no session"
    db_session.add_all([device, node])
    await db_session.commit()

    monkeypatch.setattr(_svc, "probe_session_direct", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={}))

    state = await run_session_viability_probe(
        db_session,
        device,
        checked_by=session_viability.SessionViabilityCheckedBy.scheduled,
        retry_after_viability_failure=True,
    )
    assert state["status"] == "passed"


async def test_scheduled_probe_without_retry_flag_still_rejects_viability_offline(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The carve-out must be opt-in: a plain scheduled probe on a
    viability-parked device keeps refusing."""
    device, node = _make_viability_device(db_host, "retry-flagless")
    device.session_viability_status = "failed"
    db_session.add_all([device, node])
    await db_session.commit()

    monkeypatch.setattr(_svc, "probe_session_direct", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={}))

    with pytest.raises(SessionViabilityProbeNotPermittedError):
        await run_session_viability_probe(
            db_session, device, checked_by=session_viability.SessionViabilityCheckedBy.scheduled
        )


@pytest.mark.parametrize(
    "spoiler",
    ["checks_failed", "maintenance", "stop_in_flight", "reserved"],
)
async def test_scheduled_retry_rejects_states_not_owned_by_the_series(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    spoiler: str,
) -> None:
    """The retry flag never widens admission past 'offline solely from the
    viability column': failed device checks, maintenance, an in-flight stop,
    and a reservation all still refuse."""
    device, node = _make_viability_device(db_host, f"retry-{spoiler}")
    device.session_viability_status = "failed"
    db_session.add_all([device, node])
    await db_session.flush()
    if spoiler == "checks_failed":
        device.device_checks_healthy = False
    elif spoiler == "maintenance":
        set_maintenance_reason(device, "operator")
    elif spoiler == "stop_in_flight":
        node.desired_state = AppiumDesiredState.stopped
        node.desired_port = None
    await db_session.commit()
    if spoiler == "reserved":
        await create_reservation(db_session, device_id=device.id)
        await db_session.commit()

    monkeypatch.setattr(_svc, "probe_session_direct", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={}))

    with pytest.raises(SessionViabilityProbeNotPermittedError):
        await run_session_viability_probe(
            db_session,
            device,
            checked_by=session_viability.SessionViabilityCheckedBy.scheduled,
            retry_after_viability_failure=True,
        )


_SERIES_SETTINGS = {
    "general.session_viability_failure_threshold": 3,
    "general.session_viability_timeout_sec": 10,
}


async def test_scheduled_series_escalates_within_one_pass(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three back-to-back failures reach the threshold inside one series and
    escalate exactly once, with the retry delay between attempts. Runs with the
    default ``deadline=None``, so it also pins that a deadline-less series is
    unbounded — all three attempts and both sleeps happen (this absorbed a
    separate without-a-deadline twin that asserted nothing more)."""
    device, node = _make_viability_device(db_host, "series-fail")
    db_session.add_all([device, node])
    await db_session.commit()

    handler = AsyncMock()
    sleeper = AsyncMock()
    monkeypatch.setattr(session_viability.asyncio, "sleep", sleeper)
    probe = AsyncMock(return_value=(False, "no session"))
    monkeypatch.setattr(_svc, "probe_session_direct", probe)
    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={}))
    _svc.configure_health_failure_handler(handler)
    try:
        state = await _run_scheduled_probe_series(
            db_session, device, settings=FakeSettingsReader(dict(_SERIES_SETTINGS))
        )
    finally:
        _svc.configure_health_failure_handler(None)

    assert state is not None and state["status"] == "failed"
    assert state["consecutive_failures"] == 3
    assert probe.await_count == 3
    handler.assert_awaited_once()
    assert sleeper.await_count == 2
    assert all(call.args[0] == session_viability.SCHEDULED_PROBE_RETRY_DELAY_SEC for call in sleeper.await_args_list)


async def test_scheduled_series_stops_on_first_pass(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient hiccup resolves within the series: fail, pass, stop — no
    escalation, counter reset."""
    device, node = _make_viability_device(db_host, "series-hiccup")
    db_session.add_all([device, node])
    await db_session.commit()

    handler = AsyncMock()
    monkeypatch.setattr(session_viability.asyncio, "sleep", AsyncMock())
    probe = AsyncMock(side_effect=[(False, "hiccup"), (True, None)])
    monkeypatch.setattr(_svc, "probe_session_direct", probe)
    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={}))
    _svc.configure_health_failure_handler(handler)
    try:
        state = await _run_scheduled_probe_series(
            db_session, device, settings=FakeSettingsReader(dict(_SERIES_SETTINGS))
        )
    finally:
        _svc.configure_health_failure_handler(None)

    assert state is not None and state["status"] == "passed"
    assert state["consecutive_failures"] == 0
    assert probe.await_count == 2
    handler.assert_not_awaited()


async def test_scheduled_series_counts_residual_failures_toward_the_threshold(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The threshold is counted over the persisted consecutive-failure counter:
    with two residual failures, the first fresh failure escalates and the
    series stops without burning the remaining attempts."""
    device, node = _make_viability_device(db_host, "series-residual")
    db_session.add_all([device, node])
    await db_session.commit()
    await set_session_viability_control_plane_entry(
        db_session,
        str(device.id),
        {
            "status": "failed",
            "last_attempted_at": "2020-01-01T00:00:00+00:00",
            "last_succeeded_at": None,
            "error": "old failure",
            "checked_by": "recovery",
            "consecutive_failures": 2,
        },
    )

    handler = AsyncMock()
    sleeper = AsyncMock()
    monkeypatch.setattr(session_viability.asyncio, "sleep", sleeper)
    probe = AsyncMock(return_value=(False, "still broken"))
    monkeypatch.setattr(_svc, "probe_session_direct", probe)
    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={}))
    _svc.configure_health_failure_handler(handler)
    try:
        state = await _run_scheduled_probe_series(
            db_session, device, settings=FakeSettingsReader(dict(_SERIES_SETTINGS))
        )
    finally:
        _svc.configure_health_failure_handler(None)

    assert state is not None and state["consecutive_failures"] == 3
    assert probe.await_count == 1
    handler.assert_awaited_once()
    sleeper.assert_not_awaited()


async def test_scheduled_series_stops_when_an_attempt_is_not_permitted(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A state change under the series (probe raced in, device left the
    probeable state) ends the series with the last result — no crash, and the
    retry flag is only set for attempts after the first."""
    device, node = _make_viability_device(db_host, "series-race")
    db_session.add_all([device, node])
    await db_session.commit()

    first = {"status": "failed", "consecutive_failures": 1}
    inner = AsyncMock(side_effect=[first, SessionViabilityProbeNotPermittedError("state changed")])
    monkeypatch.setattr(_svc, "run_session_viability_probe", inner)
    monkeypatch.setattr(session_viability.asyncio, "sleep", AsyncMock())

    state = await _run_scheduled_probe_series(db_session, device, settings=FakeSettingsReader(dict(_SERIES_SETTINGS)))

    assert state is first
    assert inner.await_count == 2
    assert inner.await_args_list[0].kwargs["retry_after_viability_failure"] is False
    assert inner.await_args_list[1].kwargs["retry_after_viability_failure"] is True


async def test_scheduled_series_logs_when_it_ends_early(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An early exit names the device and the cause: a raced-in probe, a state
    change and a lapsed readiness are otherwise indistinguishable from outside,
    so a lab where auto-recovery always wins the race looks exactly like one
    where the series works.

    NOTE: spy on ``logger.info`` directly rather than through ``caplog`` — the
    idiom this repo uses for log contracts (see test_maintenance_service_exit),
    because stdlib logging state left by other tests in the same xdist worker
    can keep the record from ever reaching a handler.
    """
    device, node = _make_viability_device(db_host, "series-earlylog")
    db_session.add_all([device, node])
    await db_session.commit()

    first = {"status": "failed", "consecutive_failures": 1}
    inner = AsyncMock(side_effect=[first, SessionViabilityProbeInProgressError("probe already running")])
    monkeypatch.setattr(_svc, "run_session_viability_probe", inner)
    monkeypatch.setattr(session_viability.asyncio, "sleep", AsyncMock())

    with patch.object(session_viability.logger, "info") as log_spy:
        state = await _run_scheduled_probe_series(
            db_session, device, settings=FakeSettingsReader(dict(_SERIES_SETTINGS))
        )

    assert state is first
    log_spy.assert_called_once()
    assert log_spy.call_args.args[1] == device.id
    assert log_spy.call_args.args[2] == "SessionViabilityProbeInProgressError"


async def test_scheduled_series_stops_at_the_pass_deadline(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deadline already elapsed when the first attempt fails truncates the
    series there: no retry sleep, no further attempt. This is what keeps the
    pass budget from being overrun by a whole series."""
    device, node = _make_viability_device(db_host, "series-deadline")
    db_session.add_all([device, node])
    await db_session.commit()

    sleeper = AsyncMock()
    monkeypatch.setattr(session_viability.asyncio, "sleep", sleeper)
    probe = AsyncMock(return_value=(False, "no session"))
    monkeypatch.setattr(_svc, "probe_session_direct", probe)
    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={}))

    truncated_before = _counter_total("gridfleet_session_viability_truncated_total")
    state = await _run_scheduled_probe_series(
        db_session,
        device,
        settings=FakeSettingsReader(dict(_SERIES_SETTINGS)),
        deadline=time.monotonic() - 1.0,
    )
    truncated_after = _counter_total("gridfleet_session_viability_truncated_total")

    assert state is not None and state["status"] == "failed"
    assert state["consecutive_failures"] == 1
    assert probe.await_count == 1
    sleeper.assert_not_awaited()
    # One series, cut short once: the counter counts series, not attempts.
    assert truncated_after - truncated_before == 1.0


def _series_service_with_real_health(db: AsyncSession) -> SessionViabilityService:
    """A service whose health writer is the real DeviceHealthService, so a
    failed attempt genuinely parks the device row (offline projection) and the
    retry carve-out is exercised end-to-end."""
    return SessionViabilityService(
        publisher=_test_event_bus,
        settings=FakeSettingsReader(dict(_SERIES_SETTINGS)),
        session_factory=async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False),
        capability=DeviceCapabilityService(),
        health=DeviceHealthService(publisher=_test_event_bus),
    )


async def test_scheduled_series_retries_through_the_park_and_restores(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end hiccup: attempt 1 fails and parks the device (real column
    write → offline projection); attempt 2 is still admitted, passes, and
    restores the row. Kills any regression of the retry carve-out."""
    device, node = _make_viability_device(db_host, "integ-hiccup")
    db_session.add_all([device, node])
    await db_session.commit()

    svc = _series_service_with_real_health(db_session)
    probe = AsyncMock(side_effect=[(False, "hiccup"), (True, None)])
    monkeypatch.setattr(svc, "probe_session_direct", probe)
    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={}))
    monkeypatch.setattr(session_viability.asyncio, "sleep", AsyncMock())

    state = await svc.run_scheduled_probe_series(device.id)

    assert state is not None and state["status"] == "passed"
    assert state["consecutive_failures"] == 0
    assert probe.await_count == 2
    await db_session.refresh(device)
    assert device.session_viability_status == "passed"


async def test_scheduled_series_exhaustion_parks_and_escalates_once(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end genuine breakage: all attempts fail, the device row ends
    parked, and the health-failure handler fires exactly once."""
    device, node = _make_viability_device(db_host, "integ-broken")
    db_session.add_all([device, node])
    await db_session.commit()

    svc = _series_service_with_real_health(db_session)
    handler = AsyncMock()
    svc.configure_health_failure_handler(handler)
    probe = AsyncMock(return_value=(False, "no session"))
    monkeypatch.setattr(svc, "probe_session_direct", probe)
    monkeypatch.setattr(DeviceCapabilityService, "get_device_capabilities", AsyncMock(return_value={}))
    monkeypatch.setattr(session_viability.asyncio, "sleep", AsyncMock())

    state = await svc.run_scheduled_probe_series(device.id)

    assert state is not None and state["status"] == "failed"
    assert state["consecutive_failures"] == 3
    assert probe.await_count == 3
    handler.assert_awaited_once()
    await db_session.refresh(device)
    assert device.session_viability_status == "failed"
