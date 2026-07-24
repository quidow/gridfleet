from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.appium_nodes.exceptions import NodeManagerError
from app.core.errors import AgentCallError
from app.devices.models import ConnectionType, Device, DeviceIntent, DeviceType
from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.intent_types import verification_intent_source
from app.devices.services.service import DeviceCrudService
from app.verification.services import execution
from app.verification.services.execution import (
    AgentCallContext,
    VerificationExecutionService,
)
from app.verification.services.preparation import PreparedVerificationEffect
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus
from tests.verification._lease_helpers import register_verification_node_intent

if TYPE_CHECKING:
    from app.hosts.models import Host


def _make_svc(**overrides: object) -> VerificationExecutionService:
    defaults: dict[str, object] = {
        "publisher": event_bus,
        "agent": AgentCallContext(settings=FakeSettingsReader({}), circuit_breaker=Mock()),
        "crud": DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus),
        "viability": AsyncMock(),
        "capability": DeviceCapabilityService(),
        "reconciler": AsyncMock(),
        "node_manager": AsyncMock(),
        "review": build_review_service(),
    }
    defaults.update(overrides)
    return VerificationExecutionService(**defaults)  # type: ignore[arg-type]


def _effect(
    *,
    device_id: uuid.UUID | None,
    mode: str = "update",
    operation_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    original_fields: dict[str, Any] | None = None,
    host_id: uuid.UUID | None = None,
    pack_id: str = "appium-uiautomator2",
    platform_id: str = "android_mobile",
) -> PreparedVerificationEffect:
    return PreparedVerificationEffect(
        operation_id=operation_id or uuid.uuid4(),
        mode=mode,  # type: ignore[arg-type]
        device_id=device_id,
        payload=payload if payload is not None else {},
        original_fields=original_fields,
        host_id=host_id or uuid.uuid4(),
        host_ip="10.0.0.1",
        host_agent_port=5100,
        pack_id=pack_id,
        pack_release="1.0.0",
        platform_id=platform_id,
        resolution_action=None,
    )


async def test_run_device_health_success_failure_and_agent_error(monkeypatch: pytest.MonkeyPatch) -> None:
    job: dict[str, object] = {"stages": []}
    monkeypatch.setattr(execution, "set_stage", AsyncMock())
    settings = FakeSettingsReader({"appium.startup_timeout_sec": 30})
    fetch = AsyncMock(return_value={"healthy": True})
    monkeypatch.setattr(execution, "fetch_pack_device_health", fetch)
    svc = _make_svc(agent=AgentCallContext(settings=settings, circuit_breaker=Mock()))
    effect = _effect(
        device_id=uuid.uuid4(),
        payload={
            "device_type": DeviceType.emulator,
            "connection_type": ConnectionType.usb,
            "connection_target": "target",
            "identity_value": "target",
            "ip_address": None,
        },
    )

    assert await svc.run_device_health(job, effect, http_client_factory=MagicMock()) is None

    fetch.return_value = {"healthy": False, "checks": [{"check_id": "boot_completed", "ok": False, "message": "no"}]}
    assert await svc.run_device_health(job, effect, http_client_factory=MagicMock()) == "boot completed failed (no)"
    failure_probe_args = fetch.await_args_list[1].kwargs
    assert failure_probe_args["device_type"] == "emulator"
    assert "allow_boot" not in failure_probe_args
    assert "headless" not in failure_probe_args

    fetch.side_effect = AgentCallError("10.0.0.1", "down")
    assert (
        await svc.run_device_health(job, effect, http_client_factory=MagicMock()) == "Agent health check failed: down"
    )


async def test_run_device_health_accepts_plain_str_enum_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a payload with plain-string device_type / connection_type (from an
    agent-normalized save payload) must not crash run_device_health with AttributeError.
    """
    job: dict[str, object] = {"stages": []}
    monkeypatch.setattr(execution, "set_stage", AsyncMock())
    settings = FakeSettingsReader({"appium.startup_timeout_sec": 30})
    fetch = AsyncMock(return_value={"healthy": True})
    monkeypatch.setattr(execution, "fetch_pack_device_health", fetch)
    svc = _make_svc(agent=AgentCallContext(settings=settings, circuit_breaker=Mock()))
    effect = _effect(
        device_id=uuid.uuid4(),
        payload={
            "device_type": "real_device",
            "connection_type": "usb",
            "connection_target": "target",
            "identity_value": "target",
        },
    )

    assert await svc.run_device_health(job, effect, http_client_factory=MagicMock()) is None
    assert fetch.await_args.kwargs["device_type"] == "real_device"
    assert fetch.await_args.kwargs["connection_type"] == "usb"


async def test_execute_verification_effect_missing_id_and_crash_path(monkeypatch: pytest.MonkeyPatch) -> None:
    job: dict[str, object] = {"stages": []}
    svc = _make_svc()
    with pytest.raises(NodeManagerError, match="no persisted device id"):
        await svc.execute_verification_effect(job, _effect(device_id=None), http_client_factory=MagicMock())

    svc2 = _make_svc()
    finalize = AsyncMock(return_value=execution.VerificationExecutionOutcome(status="failed"))
    monkeypatch.setattr(svc2, "_finalize_failure", finalize)
    svc2.run_device_health = AsyncMock(side_effect=RuntimeError("crash"))  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="crash"):
        await svc2.execute_verification_effect(
            job, _effect(device_id=uuid.uuid4(), mode="create"), http_client_factory=MagicMock()
        )
    finalize.assert_awaited_once()


@pytest.mark.db
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_finalize_failure_create_deletes_device_update_restores_fields(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(execution, "set_stage", AsyncMock())
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)

    # Create-mode failure deletes only the token-matching Device.
    device_c = await create_device(db_session, host_id=db_host.id, name="fail-create")
    op_c = uuid.uuid4()
    await register_verification_node_intent(
        db_session, device_c, settings=FakeSettingsReader({}), publisher=event_bus, operation_id=op_c
    )
    await db_session.commit()
    svc = _make_svc(session_factory=session_factory)
    outcome = await svc._finalize_failure(
        _effect(device_id=device_c.id, mode="create", operation_id=op_c, host_id=db_host.id),
        error="bad",
        job={"stages": []},
        node_id=None,
    )
    assert outcome.status == "failed"
    assert outcome.device_id is None
    async with session_factory() as db:
        assert await db.get(Device, device_c.id) is None

    # Update-mode failure restores the copied original fields and shelves for review.
    device_u = await create_device(db_session, host_id=db_host.id, name="renamed-during-verify")
    op_u = uuid.uuid4()
    await register_verification_node_intent(
        db_session, device_u, settings=FakeSettingsReader({}), publisher=event_bus, operation_id=op_u
    )
    await db_session.commit()
    outcome = await svc._finalize_failure(
        _effect(
            device_id=device_u.id,
            mode="update",
            operation_id=op_u,
            host_id=db_host.id,
            original_fields={"name": "original-name"},
        ),
        error="probe failed",
        job={"stages": []},
        node_id=None,
    )
    assert outcome.device_id == str(device_u.id)
    async with session_factory() as db:
        restored = await db.get(Device, device_u.id)
        assert restored is not None
        assert restored.name == "original-name"
        assert restored.review_required is True


@pytest.mark.db
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_finalize_success_verifies_and_revokes_lease(
    db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(execution, "set_stage", AsyncMock())
    session_factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    device = await create_device(db_session, host_id=db_host.id, name="verify-ok", verified_at=None)
    op = uuid.uuid4()
    await register_verification_node_intent(
        db_session, device, settings=FakeSettingsReader({}), publisher=event_bus, operation_id=op
    )
    await db_session.commit()

    svc = _make_svc(session_factory=session_factory)
    outcome = await svc._finalize_success(
        _effect(device_id=device.id, mode="create", operation_id=op, host_id=db_host.id, payload={}),
        job={"stages": []},
        node_id=None,
    )
    assert outcome.status == "completed"
    assert outcome.superseded is False
    async with session_factory() as db:
        refreshed = await db.get(Device, device.id)
        assert refreshed is not None
        assert refreshed.verified_at is not None
        lease = (
            await db.execute(
                select(DeviceIntent).where(
                    DeviceIntent.device_id == device.id,
                    DeviceIntent.source == verification_intent_source(device.id),
                )
            )
        ).scalar_one_or_none()
        assert lease is None, "a passing finalize revokes the verification lease"
