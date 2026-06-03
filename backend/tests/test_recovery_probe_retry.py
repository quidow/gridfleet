from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.lifecycle.services.incidents import LifecycleIncidentService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


def _make_svc(viability: object) -> object:
    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.lifecycle.services.policy import LifecyclePolicyService
    from app.runs.service_reservation import RunReservationService

    return LifecyclePolicyService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=event_bus,
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
        viability=viability,  # type: ignore[arg-type]
        node_manager=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_recovery_probe_stops_on_first_success() -> None:
    from app.lifecycle.services import policy as lifecycle_policy

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
    from app.lifecycle.services import policy as lifecycle_policy

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
    from app.lifecycle.services import policy as lifecycle_policy

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
async def test_recovery_probe_treats_unexpected_exception_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected probe error must not propagate out of ``_run_recovery_probe``.

    The gate ``ValueError`` is gone (verifying is admitted), but other errors — a
    concurrent state change, an unexpected bug — could still escape and crash the whole
    ``device_recovery`` job, leaving the device stranded in ``verifying`` until the
    lease's ``expires_at`` fires. Fold the error into a failed result so the retry loop
    re-probes and the caller's failure terminal applies backoff instead. (The
    ``already in progress`` collision is the one exception: it is handled separately as a
    *skip*, not a failure — see ``SessionViabilityProbeInProgressError``.)
    """
    from app.lifecycle.services import policy as lifecycle_policy

    monkeypatch.setattr(lifecycle_policy, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(lifecycle_policy, "RECOVERY_PROBE_JITTER_MAX_SEC", 0)

    probe_mock = AsyncMock(side_effect=RuntimeError("grid exploded"))
    viability = Mock()
    viability.run_session_viability_probe = probe_mock
    svc = _make_svc(viability)

    with patch.object(lifecycle_policy, "_reload_device", AsyncMock(side_effect=lambda db, dev: dev)):
        result = await svc._run_recovery_probe(SimpleNamespace(), SimpleNamespace(id="dev-1"))

    assert result["status"] == "failed"
    assert "grid exploded" in result["error"]
    # Folded into a failed result, so the existing retry loop re-probes it.
    assert probe_mock.await_count == lifecycle_policy.RECOVERY_PROBE_ATTEMPTS


@pytest.mark.asyncio
async def test_recovery_probe_treats_not_permitted_as_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate rejection (``SessionViabilityProbeNotPermittedError``) must be a *skip*, not a
    failure. A device that goes ``offline`` while reserved to an active run keeps its
    reservation row; the recovery probe now admits it, but if the device has meanwhile left
    a probeable state the gate rejects it. Folding that into a failed attempt would feed
    backoff/review and shelve a healthy device — exactly the dead-lock this fixes. Mirrors
    the ``already in progress`` collision: skip and let the lifecycle loop retry later."""
    from app.lifecycle.services import policy as lifecycle_policy
    from app.sessions.service_viability import SessionViabilityProbeNotPermittedError

    monkeypatch.setattr(lifecycle_policy, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(lifecycle_policy, "RECOVERY_PROBE_JITTER_MAX_SEC", 0)

    probe_mock = AsyncMock(side_effect=SessionViabilityProbeNotPermittedError("device not probeable"))
    viability = Mock()
    viability.run_session_viability_probe = probe_mock
    svc = _make_svc(viability)

    with patch.object(lifecycle_policy, "_reload_device", AsyncMock(side_effect=lambda db, dev: dev)):
        result = await svc._run_recovery_probe(SimpleNamespace(), SimpleNamespace(id="dev-1"))

    assert result == {"status": "skipped"}
    # No retry — a gate rejection will not clear within the short retry window (like the
    # in-progress collision), so the loop exits immediately and the policy loop retries later.
    assert probe_mock.await_count == 1


@pytest.mark.asyncio
async def test_recovery_probe_uses_viability_service() -> None:
    """The recovery probe must call self._viability.run_session_viability_probe
    without forwarding publisher (which is stored on the service itself).
    The viability service is responsible for using its own publisher."""
    from app.lifecycle.services import policy as lifecycle_policy

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
    from app.events.protocols import EventPublisher
    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.lifecycle.services.policy import LifecyclePolicyService
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
        review=build_review_service(),
        publisher=publisher,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=publisher,
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
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
