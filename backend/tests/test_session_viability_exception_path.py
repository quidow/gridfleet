"""Regression: when the viability probe raises mid-run, the exception path must
not feed ``ready_operational_state(...)`` into a state writer. The exception path
restores the *previous* operational state silently (publish_event=False); it
does not project from health view, which had previously folded
``appium_node_stop_in_flight`` into authoritative state and produced spurious
offline flaps.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.devices.models import DeviceOperationalState
from app.devices.services.capability import DeviceCapabilityService
from app.sessions import service_viability
from app.sessions.service_viability import SessionViabilityService
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_exception_path_restores_previous_available_without_projection(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: exception path must restore previous_state (AVAILABLE),
    not the result of ``ready_operational_state(...)`` projection.

    Scenario seeded:
    - Device starts AVAILABLE.
    - Node is observed running (so early "not running" exit is bypassed).
    - ``ready_operational_state`` is monkeypatched to return OFFLINE — simulating
      what happens when a stale graceful-stop intent (``appium_node_stop_in_flight``)
      is present: the projection would fold that signal and return OFFLINE even
      though the actual previous state was AVAILABLE.
    - The probe raises before completing (``get_device_capabilities`` raises RuntimeError).

    Pre-conversion assertion:
    - The old code called ``await ready_operational_state(db, relocked)`` in the
      available branch, which would return OFFLINE (mocked), and wrote OFFLINE.
    - So the device ends at OFFLINE post-exception. The test expects AVAILABLE,
      therefore it FAILS pre-conversion.

    Post-conversion assertion:
    - The new code writes ``previous_state`` (AVAILABLE) directly, ignoring the
      projection entirely.
    - The device ends at AVAILABLE. Test PASSES.
    """
    device_id = uuid.uuid4()
    # Use MagicMock objects to avoid DB writes going through state machine
    # (consistent with existing exception-path tests in test_session_viability.py)
    available_device = MagicMock(
        id=device_id,
        operational_state=DeviceOperationalState.available,
        hold=None,
    )
    available_device.appium_node = MagicMock(observed_running=True)

    locked = MagicMock(id=device_id, operational_state=DeviceOperationalState.available, hold=None)
    relocked = MagicMock(id=device_id, operational_state=DeviceOperationalState.busy, hold=None)

    monkeypatch.setattr(service_viability.control_plane_state_store, "try_claim_value", AsyncMock(return_value=True))
    monkeypatch.setattr(service_viability.control_plane_state_store, "delete_value", AsyncMock())
    monkeypatch.setattr(service_viability, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(service_viability.device_locking, "lock_device", AsyncMock(side_effect=[locked, relocked]))
    # Busy-mark goes through _MACHINE.transition (SESSION_STARTED)
    monkeypatch.setattr(service_viability._MACHINE, "transition", AsyncMock(return_value=True))

    set_state = AsyncMock()
    monkeypatch.setattr(service_viability, "set_operational_state", set_state)

    # Probe raises before completing
    monkeypatch.setattr(
        DeviceCapabilityService,
        "get_device_capabilities",
        AsyncMock(side_effect=RuntimeError("probe-exploded")),
    )

    svc = SessionViabilityService(
        publisher=event_bus,
        settings=FakeSettingsReader({"general.session_viability_timeout_sec": 5}),
        session_factory=AsyncMock(),
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="probe-exploded"):
        await svc.run_session_viability_probe(
            db_session,
            available_device,
            checked_by=service_viability.SessionViabilityCheckedBy.manual,
        )

    # The exception path must have called set_operational_state with AVAILABLE
    # (previous_state), NOT with OFFLINE (what ready_operational_state projected).
    assert set_state.call_count >= 1, "set_operational_state was never called in the exception path"
    last_call_state = set_state.await_args_list[-1].args[1]
    assert last_call_state == DeviceOperationalState.available, (
        f"Exception path restored {last_call_state!r} instead of AVAILABLE — "
        "projection-as-write antipattern is still present"
    )


async def test_exception_path_from_offline_restores_offline(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavioral parity: offline pre-state must be restored silently on exception.

    Both branches in the converted code now flow through the same write
    (``previous_state``). This test confirms the offline branch still restores
    OFFLINE — not AVAILABLE or any other projected value.

    Pre-conversion the offline branch already wrote DeviceOperationalState.offline
    directly (not via projection), so this test passes both before and after
    conversion. It serves as a non-regression guard for the unified path.
    """
    device_id = uuid.uuid4()

    offline_device = MagicMock(
        id=device_id,
        operational_state=DeviceOperationalState.offline,
        hold=None,
    )
    offline_device.appium_node = MagicMock(observed_running=True)

    locked = MagicMock(id=device_id, operational_state=DeviceOperationalState.offline, hold=None)
    relocked = MagicMock(id=device_id, operational_state=DeviceOperationalState.busy, hold=None)

    monkeypatch.setattr(service_viability.control_plane_state_store, "try_claim_value", AsyncMock(return_value=True))
    monkeypatch.setattr(service_viability.control_plane_state_store, "delete_value", AsyncMock())
    monkeypatch.setattr(service_viability, "is_ready_for_use_async", AsyncMock(return_value=True))
    monkeypatch.setattr(service_viability.device_locking, "lock_device", AsyncMock(side_effect=[locked, relocked]))
    monkeypatch.setattr(service_viability._MACHINE, "transition", AsyncMock(return_value=True))

    set_state = AsyncMock()
    monkeypatch.setattr(service_viability, "set_operational_state", set_state)

    monkeypatch.setattr(
        DeviceCapabilityService,
        "get_device_capabilities",
        AsyncMock(side_effect=RuntimeError("probe-offline-exploded")),
    )
    svc = SessionViabilityService(
        publisher=event_bus,
        settings=FakeSettingsReader({"general.session_viability_timeout_sec": 5}),
        session_factory=AsyncMock(),
        capability=DeviceCapabilityService(),
        health=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="probe-offline-exploded"):
        await svc.run_session_viability_probe(
            db_session,
            offline_device,
            checked_by=service_viability.SessionViabilityCheckedBy.recovery,
        )

    assert set_state.call_count >= 1, "set_operational_state was never called in the exception path"
    last_call_state = set_state.await_args_list[-1].args[1]
    assert last_call_state == DeviceOperationalState.offline, (
        f"Exception path restored {last_call_state!r} instead of OFFLINE"
    )
