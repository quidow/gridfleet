from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumNode
from app.core.errors import AgentCallError, AgentUnreachableError
from app.devices import locking as device_locking
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.models.intent import DeviceIntent
from app.devices.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate
from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.intent_types import verification_intent_source
from app.devices.services.service import DeviceCrudService
from app.lifecycle.services import remediation_log
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME
from app.sessions.service_probes import PROBE_CHECKED_BY_CAP_KEY
from app.sessions.service_viability import build_probe_capabilities
from app.verification.services import execution, preparation
from app.verification.services.execution import (
    AgentCallContext,
    NodeEffectSnapshot,
    VerificationExecutionService,
)
from app.verification.services.job_state import new_job
from app.verification.services.preparation import (
    PreparedVerificationEffect,
    VerificationPreparationService,
    _PackCoords,
)
from app.verification.services.service import VerificationService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device_record
from tests.helpers import test_event_bus as event_bus
from tests.verification._lease_helpers import register_verification_node_intent

if TYPE_CHECKING:
    from app.hosts.models import Host

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _job() -> dict[str, object]:
    return new_job("unit-job")


def _session_factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)


def _exec(
    *,
    session_factory: object,
    settings: object | None = None,
    viability: object | None = None,
    node_manager: object | None = None,
    reconciler: object | None = None,
    capability: object | None = None,
) -> VerificationExecutionService:
    return VerificationExecutionService(
        review=build_review_service(),
        publisher=event_bus,
        agent=AgentCallContext(settings=settings or FakeSettingsReader({}), circuit_breaker=Mock()),
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
        viability=viability if viability is not None else AsyncMock(),
        capability=capability if capability is not None else DeviceCapabilityService(),
        reconciler=reconciler if reconciler is not None else AsyncMock(),
        node_manager=node_manager if node_manager is not None else AsyncMock(),
        session_factory=session_factory,  # type: ignore[arg-type]
    )


def _effect_for(
    device: Device,
    operation_id: uuid.UUID,
    *,
    mode: str = "update",
    payload: dict[str, Any] | None = None,
    original_fields: dict[str, Any] | None = None,
) -> PreparedVerificationEffect:
    return PreparedVerificationEffect(
        operation_id=operation_id,
        mode=mode,  # type: ignore[arg-type]
        device_id=device.id,
        payload=payload if payload is not None else {},
        original_fields=original_fields,
        host_id=device.host_id,
        host_ip="10.0.0.20",
        host_agent_port=5100,
        pack_id=device.pack_id,
        pack_release="1.0.0",
        platform_id=device.platform_id,
        resolution_action=None,
    )


async def test_device_verification_job_lookup_guards() -> None:
    svc = VerificationService()
    assert await svc.get_verification_job("not-a-uuid", session_factory=AsyncMock()) is None

    class SessionCtx:
        async def __aenter__(self) -> SessionCtx:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, *_args: object) -> object:
            return SimpleNamespace(kind="other", snapshot={"status": "queued"})

    assert await svc.get_verification_job(str(uuid.uuid4()), session_factory=SessionCtx) is None


async def test_register_verification_node_intent_suppresses_stop_directive(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A verification lease structurally suppresses a derived stop directive."""
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-revoke-healthfail",
        connection_target="verify-revoke-healthfail",
        name="Verify Revoke HealthFail",
        operational_state=DeviceOperationalState.offline,
    )
    await remediation_log.append_action(
        db_session,
        device.id,
        source="health_check_fail",
        action=remediation_log.ACTION_AUTO_STOP_COMMISSIONED,
        reason="stale stop",
    )

    await register_verification_node_intent(db_session, device, settings=FakeSettingsReader({}), publisher=event_bus)

    sources = set(
        (await db_session.scalars(select(DeviceIntent.source).where(DeviceIntent.device_id == device.id))).all()
    )
    assert verification_intent_source(device.id) in sources
    ladder = await remediation_log.load_ladder(db_session, device.id)
    assert ladder.node_directive is not None
    assert ladder.node_directive.kind == remediation_log.DIRECTIVE_STOP


async def test_stop_existing_node_failure_returns_detail(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The update path's stop-of-existing-node failure surfaces a node_start error."""
    monkeypatch.setattr("app.verification.services.execution.set_stage", AsyncMock())
    session_factory = _session_factory(db_session)
    existing = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-existing-node",
        connection_target="verify-existing-node",
        name="Verify Existing",
        operational_state=DeviceOperationalState.available,
    )
    db_session.add(AppiumNode(device_id=existing.id, port=4723, pid=1, active_connection_target="live"))
    await db_session.commit()

    monkeypatch.setattr(
        "app.verification.services.execution._stop_managed_node_for_verification",
        AsyncMock(side_effect=NodeManagerError("stop failed")),
    )
    detail = await _exec(session_factory=session_factory)._stop_existing_node(
        _job(), _effect_for(existing, uuid.uuid4(), mode="update")
    )
    assert detail is not None
    assert "stop failed" in detail


async def test_run_probe_phase_probe_failure_claims_probe_row(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing probe surfaces the detail and leaves exactly one verification probe row.

    Also pins the probe contract: capabilities/timeout/target forwarded to the grid
    probe and the birth row carries the ``verification`` checked-by capability.
    """
    monkeypatch.setattr("app.verification.services.execution.set_stage", AsyncMock())
    session_factory = _session_factory(db_session)
    existing = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-probe-fail",
        connection_target="verify-probe-fail",
        name="Verify Probe Fail",
        operational_state=DeviceOperationalState.available,
    )
    node = AppiumNode(device_id=existing.id, port=4723, pid=1, active_connection_target="live")
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(node)
    monkeypatch.setattr(
        "app.devices.services.capability.DeviceCapabilityService.get_device_capabilities",
        AsyncMock(return_value={"platformName": "Android"}),
    )
    viability = AsyncMock()
    viability.probe_session_direct = AsyncMock(return_value=(False, "probe failed"))

    error = await _exec(
        session_factory=session_factory,
        settings=FakeSettingsReader({"general.session_viability_timeout_sec": 120}),
        viability=viability,
    )._run_probe_phase(
        _job(),
        _effect_for(existing, uuid.uuid4(), mode="update"),
        NodeEffectSnapshot(node.id, "live"),
    )
    assert error == "probe failed"

    viability.probe_session_direct.assert_awaited_once()
    probe_call = viability.probe_session_direct.await_args
    assert probe_call is not None
    assert probe_call.args[:2] == (build_probe_capabilities({"platformName": "Android"}), 120)
    assert probe_call.kwargs["target"] == f"http://{db_host.ip}:4723"
    assert callable(probe_call.kwargs["on_created"])

    rows = (
        (
            await db_session.execute(
                select(Session).where(Session.device_id == existing.id, Session.test_name == PROBE_TEST_NAME)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].requested_capabilities is not None
    assert rows[0].requested_capabilities[PROBE_CHECKED_BY_CAP_KEY] == "verification"


async def test_run_probe_phase_claims_with_probe_row_while_session_open(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WS-16.1: the probe claims the device with a Session row while the Appium
    probe session is open — pending at birth, promoted to the real id via
    on_created, terminal after — the orphan sweep needs no sparing branch."""
    monkeypatch.setattr("app.verification.services.execution.set_stage", AsyncMock())
    session_factory = _session_factory(db_session)
    existing = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-inflight",
        connection_target="verify-inflight",
        name="Verify Inflight",
        operational_state=DeviceOperationalState.available,
    )
    node = AppiumNode(device_id=existing.id, port=4723, pid=1, active_connection_target="live")
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(node)
    monkeypatch.setattr(
        "app.devices.services.capability.DeviceCapabilityService.get_device_capabilities",
        AsyncMock(return_value={"platformName": "Android"}),
    )

    seen_mid_probe: list[tuple[object, str]] = []

    async def _probe_direct(
        _caps: object, _timeout: int, *, target: str | None, on_created: object = None
    ) -> tuple[bool, None]:
        if on_created is not None:
            await on_created("verification-appium-id")  # type: ignore[operator]
        async with session_factory() as probe_db:
            rows = (await probe_db.execute(select(Session).where(Session.device_id == existing.id))).scalars().all()
            seen_mid_probe.extend((row.status, row.session_id) for row in rows)
        return True, None

    viability = AsyncMock()
    viability.probe_session_direct = _probe_direct

    error = await _exec(
        session_factory=session_factory,
        settings=FakeSettingsReader({"general.session_viability_timeout_sec": 120}),
        viability=viability,
    )._run_probe_phase(
        _job(),
        _effect_for(existing, uuid.uuid4(), mode="update"),
        NodeEffectSnapshot(node.id, "live"),
    )
    assert error is None
    assert seen_mid_probe == [(SessionStatus.running, "verification-appium-id")]

    rows = (await db_session.execute(select(Session).where(Session.device_id == existing.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].test_name == PROBE_TEST_NAME
    assert rows[0].status == SessionStatus.passed
    assert rows[0].ended_at is not None


async def test_run_probe_phase_finalizes_probe_row_when_probe_session_raises(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exceptions inside the probe release the birth-row claim (terminal error)."""
    monkeypatch.setattr("app.verification.services.execution.set_stage", AsyncMock())
    session_factory = _session_factory(db_session)
    existing = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-inflight-raises",
        connection_target="verify-inflight-raises",
        name="Verify Inflight Raises",
        operational_state=DeviceOperationalState.available,
    )
    node = AppiumNode(device_id=existing.id, port=4723, pid=1, active_connection_target="live")
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(node)
    monkeypatch.setattr(
        "app.devices.services.capability.DeviceCapabilityService.get_device_capabilities",
        AsyncMock(return_value={"platformName": "Android"}),
    )

    async def _probe_direct(
        _caps: object, _timeout: int, *, target: str | None, on_created: object = None
    ) -> tuple[bool, None]:
        if on_created is not None:
            await on_created("verification-appium-id")  # type: ignore[operator]
        raise RuntimeError("probe blew up")

    viability = AsyncMock()
    viability.probe_session_direct = _probe_direct

    with pytest.raises(RuntimeError, match="probe blew up"):
        await _exec(
            session_factory=session_factory,
            settings=FakeSettingsReader({"general.session_viability_timeout_sec": 120}),
            viability=viability,
        )._run_probe_phase(
            _job(),
            _effect_for(existing, uuid.uuid4(), mode="update"),
            NodeEffectSnapshot(node.id, "live"),
        )
    rows = (await db_session.execute(select(Session).where(Session.device_id == existing.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].test_name == PROBE_TEST_NAME
    assert rows[0].status == SessionStatus.error
    assert rows[0].ended_at is not None


async def test_stop_verification_node_cleanup_error_path(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-NodeManagerError while writing the stopped desired state surfaces a
    cleanup failure detail but still clears the node's observation columns."""
    monkeypatch.setattr("app.verification.services.execution.set_stage", AsyncMock())
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-save-001",
        connection_target="verify-save-001",
        name="Verify Save",
    )
    node = AppiumNode(device_id=device.id, port=4723, pid=1, active_connection_target="live")
    db_session.add(node)
    await db_session.commit()

    monkeypatch.setattr(
        "app.verification.services.execution.write_desired_state",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    cleanup_error = await execution._stop_verification_node_if_running(_job(), db_session, device, node)
    assert cleanup_error == "Failed to stop verification node: boom"
    assert node.pid is None


async def test_verification_execution_remaining_error_branches(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.verification.services.execution.set_stage", AsyncMock())
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-remaining-001",
        connection_target="verify-remaining-001",
        name="Verify Remaining",
    )

    # The helper re-loads the device under the row lock (WI-1), so it needs real
    # rows: a device with no node raises, a device with a running node stops it.
    with pytest.raises(NodeManagerError, match="No running node"):
        await execution._stop_managed_node_for_verification(db_session, device)

    node = AppiumNode(device_id=device.id, port=4723, pid=123, active_connection_target="live")
    db_session.add(node)
    await db_session.commit()
    monkeypatch.setattr("app.verification.services.execution.write_desired_state", AsyncMock())
    stopped = await execution._stop_managed_node_for_verification(db_session, device)
    assert stopped.pid is None
    assert stopped.active_connection_target is None

    # A NodeManagerError from the stopped-state write is swallowed (already stopped).
    monkeypatch.setattr(
        "app.verification.services.execution.write_desired_state",
        AsyncMock(side_effect=NodeManagerError("already stopped")),
    )
    assert await execution._stop_verification_node_if_running(_job(), db_session, device, node) is None

    target = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="restore-src",
        connection_target="restore-src",
        name="Restore Source",
        os_version="14",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    execution._restore_create_payload_fields(
        target,
        {
            "pack_id": "pack",
            "platform_id": "platform",
            "identity_scheme": "scheme",
            "identity_scope": "scope",
            "identity_value": "identity",
            "connection_target": "target",
            "name": "Restored",
            "os_version": "15",
            "manufacturer": "Maker",
            "model": "Model",
            "model_number": "M1",
            "software_versions": {"driver": "1"},
            "device_type": DeviceType.emulator,
            "connection_type": ConnectionType.virtual,
            "ip_address": None,
            "device_config": {"a": 1},
            "ignored": "value",
        },
    )
    assert target.name == "Restored"
    assert target.device_config == {"a": 1}


async def test_stop_managed_node_locks_device_before_desired_state_write(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WI-1: the verification update path's stopped-state write MUST happen under
    # the device row lock. Assert lock_device is acquired before
    # write_desired_state fires, and the observation-column clears land on the
    # locked row.
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-lock-001",
        connection_target="verify-lock-001",
        name="Verify Lock",
    )
    node = AppiumNode(device_id=device.id, port=4723, pid=123, active_connection_target="live")
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device, ["appium_node"])

    calls: list[str] = []
    real_lock = device_locking.lock_device
    real_write = execution.write_desired_state

    async def spy_lock(*args: object, **kwargs: object) -> object:
        calls.append("lock_device")
        return await real_lock(*args, **kwargs)  # type: ignore[arg-type]

    async def spy_write(*args: object, **kwargs: object) -> object:
        calls.append("write_desired_state")
        return await real_write(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("app.devices.locking.lock_device", spy_lock)
    monkeypatch.setattr("app.verification.services.execution.write_desired_state", spy_write)

    stopped = await execution._stop_managed_node_for_verification(db_session, device)

    assert calls == ["lock_device", "write_desired_state"]
    assert stopped.pid is None
    assert stopped.active_connection_target is None


def test_transport_identity_and_virtual_lane_helpers() -> None:
    assert preparation._is_transport_identity(None, None, None) is True
    assert preparation._is_transport_identity("10.0.0.5", "other", "10.0.0.5") is True
    assert preparation._is_transport_identity("stable-serial", None, None) is False
    assert preparation._payload_requests_virtual_lane({"device_type": "emulator"}) is True
    assert preparation._payload_requests_virtual_lane({"device_type": "real_device"}) is False


def _prep(db_session: AsyncSession) -> VerificationPreparationService:
    return VerificationPreparationService(
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        crud=DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
        identity=DeviceIdentityConflictService(),
        publisher=event_bus,
        session_factory=_session_factory(db_session),
    )


def _resolve_coords(*, action: str | None = "resolve") -> _PackCoords:
    return _PackCoords(
        pack_id="pack",
        pack_release="1",
        platform_id="platform",
        resolution_action=action,
    )


async def test_normalize_effect_field_errors_and_resolution_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prep = _prep(db_session)

    # Adapter field errors surface as "<field>: <message>".
    monkeypatch.setattr(
        "app.verification.services.preparation.normalize_pack_device",
        AsyncMock(return_value={"field_errors": [{"field_id": "serial", "message": "bad"}]}),
    )
    payload: dict[str, Any] = {
        "pack_id": "pack",
        "platform_id": "platform",
        "identity_value": "10.0.0.1:5555",
        "connection_target": "10.0.0.1:5555",
        "device_type": DeviceType.real_device,
        "connection_type": ConnectionType.network,
    }
    _out, error = await prep.normalize_effect(
        payload, _resolve_coords(), host_ip=db_host.ip, host_agent_port=5100, http_client_factory=object
    )
    assert error == "serial: bad"

    # A non-dict field-error entry falls back to a generic rejection message.
    monkeypatch.setattr(
        "app.verification.services.preparation.normalize_pack_device",
        AsyncMock(return_value={"field_errors": ["bad"]}),
    )
    _out, error = await prep.normalize_effect(
        dict(payload), _resolve_coords(), host_ip=db_host.ip, host_agent_port=5100, http_client_factory=object
    )
    assert error == "Adapter rejected device input"

    # A resolution action that 404s / rejects surfaces the stable-identity requirement.
    monkeypatch.setattr("app.verification.services.preparation.normalize_pack_device", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.verification.services.preparation.pack_device_lifecycle_action",
        AsyncMock(side_effect=AgentCallError("10.0.0.1", "404 resolve")),
    )
    _out, error = await prep.normalize_effect(
        dict(payload), _resolve_coords(), host_ip=db_host.ip, host_agent_port=5100, http_client_factory=object
    )
    assert error == "Device must resolve to a stable identity before save (action: resolve)"

    # A transport-level agent failure surfaces "Host resolution failed".
    monkeypatch.setattr(
        "app.verification.services.preparation.pack_device_lifecycle_action",
        AsyncMock(side_effect=AgentUnreachableError("10.0.0.20", "agent down")),
    )
    _out, error = await prep.normalize_effect(
        dict(payload), _resolve_coords(), host_ip=db_host.ip, host_agent_port=5100, http_client_factory=object
    )
    assert error == "Host resolution failed: agent down"

    # A successful resolution mutates the payload and forwards the lifecycle args.
    resolve_action = AsyncMock(
        return_value={
            "success": True,
            "identity_value": "stable",
            "connection_target": "10.0.0.1:5555",
            "name": "Resolved",
        }
    )
    monkeypatch.setattr("app.verification.services.preparation.pack_device_lifecycle_action", resolve_action)
    resolved_payload = dict(payload)
    _out, error = await prep.normalize_effect(
        resolved_payload, _resolve_coords(), host_ip=db_host.ip, host_agent_port=5100, http_client_factory=object
    )
    assert error is None
    assert resolved_payload["identity_value"] == "stable"
    assert resolved_payload["name"] == "Resolved"
    assert resolve_action.await_args.kwargs["args"] == {
        "device_type": "real_device",
        "connection_type": "network",
        "ip_address": None,
    }


async def test_normalize_effect_swallows_agent_errors_and_coerces_enums(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prep = _prep(db_session)

    # normalize agent transients (unreachable / lookup / type-mock) fall back to
    # manifest/local fields instead of failing the effect.
    for exc in (AgentCallError("10.0.0.1", "down"), LookupError("missing"), TypeError("bad mock")):
        monkeypatch.setattr(
            "app.verification.services.preparation.normalize_pack_device",
            AsyncMock(side_effect=exc),
        )
        _out, error = await prep.normalize_effect(
            {
                "pack_id": "pack",
                "platform_id": "platform",
                "identity_value": "stable",
                "connection_target": "stable",
                "device_type": DeviceType.real_device,
                "connection_type": ConnectionType.usb,
            },
            _resolve_coords(action=None),
            host_ip=db_host.ip,
            host_agent_port=5100,
            http_client_factory=object,
        )
        assert error is None

    # Regression: agent normalize returns plain-string device_type / connection_type.
    # normalize_effect must coerce them back to enum types so later setattr onto
    # Mapped[Enum] columns does not corrupt the device row.
    monkeypatch.setattr(
        "app.verification.services.preparation.normalize_pack_device",
        AsyncMock(
            return_value={
                "identity_value": "stable-serial",
                "connection_target": "stable-target",
                "os_version": "15",
                "manufacturer": "Maker",
                "model": "Model X",
                "device_type": "real_device",
                "connection_type": "network",
            }
        ),
    )
    payload: dict[str, Any] = {
        "pack_id": "pack",
        "platform_id": "platform",
        "identity_value": "10.0.0.2:5555",
        "connection_target": "10.0.0.2:5555",
        "name": "stable-serial",
        "device_type": DeviceType.real_device,
        "connection_type": ConnectionType.network,
        "ip_address": "10.0.0.2",
    }
    _out, error = await prep.normalize_effect(
        payload, _resolve_coords(action=None), host_ip=db_host.ip, host_agent_port=5100, http_client_factory=object
    )
    assert error is None
    assert payload["identity_value"] == "stable-serial"
    assert payload["manufacturer"] == "Maker"
    assert payload["name"] == "Model X"
    assert isinstance(payload["device_type"], DeviceType)
    assert isinstance(payload["connection_type"], ConnectionType)


async def test_prepare_create_validation_error_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    prep = _prep(db_session)

    # A create whose assigned host does not exist fails validation.
    bad_create = DeviceVerificationCreate(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="missing-host",
        connection_target="missing-host",
        name="Missing Host",
        os_version="14",
        host_id=uuid.uuid4(),
    )
    effect, error = await prep.prepare_create(_job(), uuid.uuid4(), bad_create, http_client_factory=object)
    assert effect is None
    assert error == "Assigned host is required"

    # A payload-prep ValueError is reported as the validation failure.
    valid_create = DeviceVerificationCreate(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="verify-bad-create",
        connection_target="verify-bad-create",
        name="Bad Create",
        os_version="14",
        host_id=db_host.id,
    )
    monkeypatch.setattr(
        "app.verification.services.preparation.device_write.prepare_device_create_payload_async",
        AsyncMock(side_effect=ValueError("bad create")),
    )
    effect, error = await prep.prepare_create(_job(), uuid.uuid4(), valid_create, http_client_factory=object)
    assert effect is None
    assert error == "bad create"


async def test_prepare_update_validation_error_paths(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())

    # A missing device fails the update precondition.
    prep = _prep(db_session)
    effect, error = await prep.prepare_update(
        _job(),
        uuid.uuid4(),
        uuid.uuid4(),
        DeviceVerificationUpdate(name="missing", host_id=db_host.id),
        http_client_factory=object,
    )
    assert effect is None
    assert error == "Device was not found"

    existing = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-update-errors",
        connection_target="verify-update-errors",
        name="Verify Update Errors",
        operational_state=DeviceOperationalState.offline,
    )

    # A payload-prep ValueError is reported as the validation failure.
    monkeypatch.setattr(
        "app.verification.services.preparation.device_write.prepare_device_update_payload_async",
        AsyncMock(side_effect=ValueError("bad update")),
    )
    effect, error = await prep.prepare_update(
        _job(),
        uuid.uuid4(),
        existing.id,
        DeviceVerificationUpdate(name="bad", host_id=existing.host_id),
        http_client_factory=object,
    )
    assert effect is None
    assert error == "bad update"

    # A resolved payload pointing at a nonexistent host fails validation.
    async def _prep_payload_missing_host(*_a: object, **_k: object) -> dict[str, Any]:
        return {"name": "renamed", "host_id": uuid.uuid4()}

    monkeypatch.setattr(
        "app.verification.services.preparation.device_write.prepare_device_update_payload_async",
        _prep_payload_missing_host,
    )
    effect, error = await prep.prepare_update(
        _job(),
        uuid.uuid4(),
        existing.id,
        DeviceVerificationUpdate(name="renamed", host_id=existing.host_id),
        http_client_factory=object,
    )
    assert effect is None
    assert error == "Assigned host is required"

    # A normalize error propagates verbatim.
    async def _prep_payload_ok(*_a: object, **_k: object) -> dict[str, Any]:
        return {"name": "renamed", "host_id": existing.host_id}

    monkeypatch.setattr(
        "app.verification.services.preparation.device_write.prepare_device_update_payload_async",
        _prep_payload_ok,
    )
    monkeypatch.setattr(
        VerificationPreparationService,
        "normalize_effect",
        AsyncMock(return_value=({}, "resolution failed")),
    )
    effect, error = await prep.prepare_update(
        _job(),
        uuid.uuid4(),
        existing.id,
        DeviceVerificationUpdate(name="renamed", host_id=existing.host_id),
        http_client_factory=object,
    )
    assert effect is None
    assert error == "resolution failed"


async def test_prepare_update_reports_identity_conflict(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate identity discovered post-normalize fails the update."""
    monkeypatch.setattr("app.verification.services.job_state.publish", AsyncMock())
    existing = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="verify-update-dup",
        connection_target="verify-update-dup",
        name="Verify Update Dup",
        operational_state=DeviceOperationalState.offline,
    )
    prep = _prep(db_session)

    async def _prep_payload_ok(*_a: object, **_k: object) -> dict[str, Any]:
        return {"name": "renamed", "host_id": existing.host_id}

    monkeypatch.setattr(
        "app.verification.services.preparation.device_write.prepare_device_update_payload_async",
        _prep_payload_ok,
    )
    monkeypatch.setattr(
        VerificationPreparationService,
        "normalize_effect",
        AsyncMock(side_effect=lambda payload, *a, **k: (payload, None)),
    )
    monkeypatch.setattr(
        prep._identity,
        "ensure_device_payload_identity_available",
        AsyncMock(side_effect=preparation.DeviceIdentityConflictError("update duplicate")),
    )
    effect, error = await prep.prepare_update(
        _job(),
        uuid.uuid4(),
        existing.id,
        DeviceVerificationUpdate(name="renamed", host_id=existing.host_id),
        http_client_factory=object,
    )
    assert effect is None
    assert error == "update duplicate"
