"""Unit tests for ``_sweep_orphaned_intents`` (deliverable D).

One positive test per orphan condition + one negative test for the
``connectivity:*`` condition (offline device must NOT be swept). Counter
increment is asserted on the active_session positive case to pin the metric
contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import metrics_recorders
from app.devices.models import DeviceIntent, DeviceOperationalState
from app.devices.services import intent_reconciler, state_write_guard
from app.devices.services.intent_reconciler import run_device_intent_reconciler_once
from app.devices.services.intent_types import GRID_ROUTING, NODE_PROCESS
from app.sessions.models import Session, SessionStatus
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device, create_reserved_run

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _intent_exists(db: AsyncSession, device_id: object, source: str) -> bool:
    from sqlalchemy import select

    row = (
        await db.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device_id,
                DeviceIntent.source == source,
            )
        )
    ).scalar_one_or_none()
    return row is not None


def _counter_value(source: str) -> float:
    # prometheus_client Counter labels: use ._value.get() per upstream test idiom.
    return float(metrics_recorders.STALE_INTENT_SWEEP_REVOKED.labels(source=source)._value.get())


# ---------------------------------------------------------------------------
# active_session:{sid}
# ---------------------------------------------------------------------------


async def test_sweep_revokes_active_session_intent_when_session_ended(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Positive: intent for a session that has ended_at IS NOT NULL must be swept.
    Also asserts the Prometheus counter increments by exactly 1."""
    device = await create_device(db_session, host_id=db_host.id, name="sweep-active-session-ended")

    session = Session(session_id="ended-sess-1", device_id=device.id, status=SessionStatus.passed)
    session.ended_at = datetime.now(UTC)
    db_session.add(session)

    source = f"active_session:{session.session_id}"
    db_session.add(DeviceIntent(device_id=device.id, source=source, axis=NODE_PROCESS, payload={}))
    await db_session.commit()

    before = _counter_value("active_session")
    await intent_reconciler._sweep_orphaned_intents(db_session)
    await db_session.commit()

    assert not await _intent_exists(db_session, device.id, source)
    assert _counter_value("active_session") == before + 1


async def test_sweep_preserves_active_session_intent_when_session_active(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Negative: intent for a session whose ended_at IS NULL must be preserved."""
    device = await create_device(db_session, host_id=db_host.id, name="sweep-active-session-live")

    session = Session(session_id="live-sess-1", device_id=device.id, status=SessionStatus.running)
    # ended_at stays None
    db_session.add(session)

    source = f"active_session:{session.session_id}"
    db_session.add(DeviceIntent(device_id=device.id, source=source, axis=NODE_PROCESS, payload={}))
    await db_session.commit()

    await intent_reconciler._sweep_orphaned_intents(db_session)
    await db_session.commit()

    assert await _intent_exists(db_session, device.id, source)


# ---------------------------------------------------------------------------
# connectivity:{device_id}
# ---------------------------------------------------------------------------


async def test_sweep_revokes_connectivity_intent_when_device_healthy(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Positive: device is available and device_checks_healthy=True → intent swept."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="sweep-connectivity-healthy",
        operational_state=DeviceOperationalState.available,
    )
    with state_write_guard.bypass():
        device.device_checks_healthy = True
    await db_session.commit()

    source = f"connectivity:{device.id}"
    db_session.add(DeviceIntent(device_id=device.id, source=source, axis=NODE_PROCESS, payload={}))
    await db_session.commit()

    await intent_reconciler._sweep_orphaned_intents(db_session)
    await db_session.commit()

    assert not await _intent_exists(db_session, device.id, source)


async def test_sweep_preserves_connectivity_intent_when_device_offline(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Negative: offline device → connectivity intent must be preserved."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="sweep-connectivity-offline",
        operational_state=DeviceOperationalState.offline,
    )

    source = f"connectivity:{device.id}"
    db_session.add(DeviceIntent(device_id=device.id, source=source, axis=NODE_PROCESS, payload={}))
    await db_session.commit()

    await intent_reconciler._sweep_orphaned_intents(db_session)
    await db_session.commit()

    assert await _intent_exists(db_session, device.id, source)


async def test_sweep_preserves_connectivity_intent_when_device_unhealthy(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Negative: available device with device_checks_healthy=False → intent preserved."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="sweep-connectivity-unhealthy",
        operational_state=DeviceOperationalState.available,
    )
    with state_write_guard.bypass():
        device.device_checks_healthy = False
    await db_session.commit()

    source = f"connectivity:{device.id}"
    db_session.add(DeviceIntent(device_id=device.id, source=source, axis=NODE_PROCESS, payload={}))
    await db_session.commit()

    await intent_reconciler._sweep_orphaned_intents(db_session)
    await db_session.commit()

    assert await _intent_exists(db_session, device.id, source)


# ---------------------------------------------------------------------------
# cooldown:{axis}:{run_id}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("axis", ["node", "grid", "reservation", "recovery"])
async def test_sweep_revokes_cooldown_intent_when_reservation_released(
    db_session: AsyncSession,
    db_host: Host,
    axis: str,
) -> None:
    """Positive: DeviceReservation.released_at IS NOT NULL → cooldown intent swept."""
    from app.runs.models import RunState

    device = await create_device(db_session, host_id=db_host.id, name=f"sweep-cooldown-released-{axis}")
    run = await create_reserved_run(
        db_session,
        name=f"released-run-{axis}",
        devices=[device],
        state=RunState.expired,
        mark_released=True,
    )

    source = f"cooldown:{axis}:{run.id}"
    db_session.add(DeviceIntent(device_id=device.id, source=source, axis=GRID_ROUTING, payload={}))
    await db_session.commit()

    await intent_reconciler._sweep_orphaned_intents(db_session)
    await db_session.commit()

    assert not await _intent_exists(db_session, device.id, source)


async def test_sweep_preserves_cooldown_intent_when_reservation_active(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Negative: DeviceReservation.released_at IS NULL → cooldown intent preserved."""
    from app.runs.models import RunState

    device = await create_device(db_session, host_id=db_host.id, name="sweep-cooldown-active")
    run = await create_reserved_run(
        db_session,
        name="active-run-cooldown",
        devices=[device],
        state=RunState.active,
        mark_released=False,
    )

    source = f"cooldown:node:{run.id}"
    db_session.add(DeviceIntent(device_id=device.id, source=source, axis=GRID_ROUTING, payload={}))
    await db_session.commit()

    await intent_reconciler._sweep_orphaned_intents(db_session)
    await db_session.commit()

    assert await _intent_exists(db_session, device.id, source)


# ---------------------------------------------------------------------------
# Sweep cadence: only on full-scan cycles
# ---------------------------------------------------------------------------


async def test_orphan_sweeps_run_only_on_full_scan_cycle(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both orphan sweeps must run on a full-scan cycle (cycle % full_scan_every == 0)
    and be skipped on a non-full-scan cycle."""
    calls: list[str] = []

    async def _spy_sweep(db: object) -> None:
        calls.append("sweep")

    async def _spy_terminal(db: object, **kwargs: object) -> None:
        calls.append("terminal")

    monkeypatch.setattr(intent_reconciler, "_sweep_orphaned_intents", _spy_sweep)
    monkeypatch.setattr(intent_reconciler, "_reconcile_terminal_run_intents", _spy_terminal)
    monkeypatch.setattr("app.devices.services.intent_reconciler.assert_current_leader", AsyncMock())

    settings = FakeSettingsReader({"general.intent_reconcile_full_scan_every_cycles": 5})

    # cycle=0: 0 % 5 == 0 → full scan → sweeps must run
    await run_device_intent_reconciler_once(
        db_session,
        cycle=0,
        settings=settings,
        circuit_breaker=Mock(),
        publisher=AsyncMock(),
    )
    assert calls.count("sweep") == 1, "sweep must run on full-scan cycle"
    assert calls.count("terminal") == 1, "terminal sweep must run on full-scan cycle"

    calls.clear()

    # cycle=1: 1 % 5 != 0 → dirty-only → sweeps must NOT run
    await run_device_intent_reconciler_once(
        db_session,
        cycle=1,
        settings=settings,
        circuit_breaker=Mock(),
        publisher=AsyncMock(),
    )
    assert calls.count("sweep") == 0, "sweep must NOT run on non-full-scan cycle"
    assert calls.count("terminal") == 0, "terminal sweep must NOT run on non-full-scan cycle"
