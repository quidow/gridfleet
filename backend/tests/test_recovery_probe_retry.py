from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


def _make_svc(viability: object) -> object:
    from app.devices.services.lifecycle_policy import LifecyclePolicyService
    from app.devices.services.lifecycle_policy_actions import LifecyclePolicyActionsService
    from app.runs.service_reservation import RunReservationService

    return LifecyclePolicyService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(publisher=event_bus, reservation=RunReservationService()),
        viability=viability,  # type: ignore[arg-type]
        node_manager=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_recovery_probe_stops_on_first_success() -> None:
    from app.devices.services import lifecycle_policy

    probe_mock = AsyncMock(return_value={"status": "passed"})
    viability = Mock()
    viability.run_session_viability_probe = probe_mock
    svc = _make_svc(viability)

    with patch.object(lifecycle_policy, "_reload_device", AsyncMock(side_effect=lambda db, dev: dev)):
        result = await svc._run_recovery_probe(SimpleNamespace(), SimpleNamespace(id="dev-1"))

    assert probe_mock.await_count == 1
    assert result == {"status": "passed"}


@pytest.mark.asyncio
async def test_recovery_probe_retries_until_attempts_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.devices.services import lifecycle_policy

    monkeypatch.setattr(lifecycle_policy, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(lifecycle_policy, "RECOVERY_PROBE_JITTER_MAX_SEC", 0)

    probe_mock = AsyncMock(return_value={"status": "failed", "error": "boom"})
    viability = Mock()
    viability.run_session_viability_probe = probe_mock
    svc = _make_svc(viability)

    with patch.object(lifecycle_policy, "_reload_device", AsyncMock(side_effect=lambda db, dev: dev)):
        result = await svc._run_recovery_probe(SimpleNamespace(), SimpleNamespace(id="dev-1"))

    assert probe_mock.await_count == lifecycle_policy.RECOVERY_PROBE_ATTEMPTS
    assert result == {"status": "failed", "error": "boom"}


@pytest.mark.asyncio
async def test_recovery_probe_retries_then_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.devices.services import lifecycle_policy

    monkeypatch.setattr(lifecycle_policy, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(lifecycle_policy, "RECOVERY_PROBE_JITTER_MAX_SEC", 0)

    outcomes: list[dict[str, Any]] = [
        {"status": "failed", "error": "x"},
        {"status": "failed", "error": "y"},
        {"status": "passed"},
    ]
    probe_mock = AsyncMock(side_effect=outcomes)
    viability = Mock()
    viability.run_session_viability_probe = probe_mock
    svc = _make_svc(viability)

    with patch.object(lifecycle_policy, "_reload_device", AsyncMock(side_effect=lambda db, dev: dev)):
        result = await svc._run_recovery_probe(SimpleNamespace(), SimpleNamespace(id="dev-1"))

    assert probe_mock.await_count == 3
    assert result == {"status": "passed"}


@pytest.mark.asyncio
async def test_recovery_probe_uses_viability_service() -> None:
    """The recovery probe must call self._viability.run_session_viability_probe
    without forwarding publisher (which is stored on the service itself).
    The viability service is responsible for using its own publisher."""
    from app.devices.services import lifecycle_policy

    probe_mock = AsyncMock(return_value={"status": "passed"})
    viability = Mock()
    viability.run_session_viability_probe = probe_mock
    svc = _make_svc(viability)

    with patch.object(lifecycle_policy, "_reload_device", AsyncMock(side_effect=lambda db, dev: dev)):
        await svc._run_recovery_probe(SimpleNamespace(), SimpleNamespace(id="dev-1"))

    # Verify the viability service method was called — publisher is encapsulated
    # in the viability service itself, not passed as a kwarg.
    probe_mock.assert_awaited_once()
    call_kwargs = probe_mock.await_args.kwargs
    assert "publisher" not in call_kwargs


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_attempt_auto_recovery_calls_run_recovery_probe(db_session: AsyncSession, db_host: Host) -> None:
    """``attempt_auto_recovery`` calls ``self._run_recovery_probe``, which
    uses ``self._viability.run_session_viability_probe``. The publisher is
    stored on the service and passed implicitly via the viability service."""
    from app.appium_nodes.models import AppiumDesiredState, AppiumNode
    from app.devices.services import state_write_guard
    from app.devices.services.lifecycle_policy import LifecyclePolicyService
    from app.devices.services.lifecycle_policy_actions import LifecyclePolicyActionsService
    from app.events.protocols import EventPublisher
    from app.runs.service_reservation import RunReservationService
    from tests.helpers import create_device

    device = await create_device(db_session, host_id=db_host.id, name="dw-publisher-forward", verified=True)
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            desired_port=4723,
            pid=12345,
            active_connection_target="127.0.0.1:4723",
            desired_state=AppiumDesiredState.running,
        )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device)

    publisher = AsyncMock(spec=EventPublisher)
    probe_called: list[bool] = []

    async def _capture_probe(self_arg: object, db: object, dev: object) -> dict[str, Any]:
        probe_called.append(True)
        return {"status": "passed"}

    svc = LifecyclePolicyService(
        publisher=publisher,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(publisher=publisher, reservation=RunReservationService()),
        viability=Mock(),
        node_manager=AsyncMock(),
    )
    with patch.object(LifecyclePolicyService, "_run_recovery_probe", new=_capture_probe):
        await svc.attempt_auto_recovery(
            db_session,
            device,
            source="connectivity",
            reason="test",
        )

    assert probe_called, "_run_recovery_probe was not called during attempt_auto_recovery"
