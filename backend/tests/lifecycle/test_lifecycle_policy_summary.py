from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.services.lifecycle_policy_state import (
    MAINTENANCE_HOLD_SUPPRESSION_REASON,
    clear_maintenance_reason,
    set_maintenance_reason,
)
from app.devices.services.lifecycle_policy_summary import (
    build_lifecycle_policy,
    build_lifecycle_policy_from_facts,
    build_lifecycle_policy_summary,
    freeze_reservation_context,
)
from app.devices.services.readiness import is_ready_for_use_async
from app.devices.services.recovery_projection import recovery_availability
from app.devices.services.state import derive_operational_state
from app.lifecycle.services import remediation_log
from app.runs import service_reservation as run_reservation_service
from tests.helpers import create_device, create_reserved_run

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.hosts.models import Host


def test_maintenance_summary_uses_maintenance_reason_instead_of_tautology() -> None:
    policy = {
        "recovery_state": "suppressed",
        "recovery_suppressed_reason": "Device is in maintenance mode",
        "maintenance_reason": "Cooldown escalation",
        "last_failure_reason": None,
        "last_failure_source": None,
        "last_action": None,
        "deferred_stop": False,
        "deferred_stop_reason": None,
        "excluded_from_run": False,
    }
    summary = build_lifecycle_policy_summary(policy)
    assert summary["detail"] == "Cooldown escalation"


def test_maintenance_summary_falls_back_when_no_maintenance_reason() -> None:
    policy = {
        "recovery_state": "suppressed",
        "recovery_suppressed_reason": "Device is in maintenance mode",
        "maintenance_reason": None,
        "last_failure_reason": None,
        "last_failure_source": None,
        "last_action": None,
        "deferred_stop": False,
        "deferred_stop_reason": None,
        "excluded_from_run": False,
    }
    summary = build_lifecycle_policy_summary(policy)
    assert summary["detail"] == "Device is in maintenance mode"


def test_non_maintenance_suppression_uses_original_detail() -> None:
    policy = {
        "recovery_state": "suppressed",
        "recovery_suppressed_reason": "Auto-manage is disabled",
        "maintenance_reason": None,
        "last_failure_reason": "Node restart failed",
        "last_failure_source": "appium_reconciler",
        "last_action": None,
        "deferred_stop": False,
        "deferred_stop_reason": None,
        "excluded_from_run": False,
    }
    summary = build_lifecycle_policy_summary(policy)
    assert summary["detail"] == "Auto-manage is disabled"


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_suppressed_badge_is_projected_from_facts(db_session: AsyncSession, db_host: Host) -> None:
    """Maintenance renders suppressed with NO stored suppression key."""
    device = await create_device(db_session, host_id=db_host.id, name="proj-maint")
    locked = await device_locking.lock_device(db_session, device.id)
    set_maintenance_reason(locked, "operator hold")
    await db_session.commit()

    policy = await build_lifecycle_policy(db_session, locked)
    assert policy["recovery_state"] == "suppressed"
    assert policy["recovery_suppressed_reason"] == MAINTENANCE_HOLD_SUPPRESSION_REASON
    # And the stored JSON never carried the key's value:
    assert (locked.lifecycle_policy_state or {}).get("recovery_suppressed_reason") is None


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_badge_clears_instantly_when_fact_clears(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="proj-clears")
    locked = await device_locking.lock_device(db_session, device.id)
    set_maintenance_reason(locked, "hold")
    await db_session.commit()
    assert (await build_lifecycle_policy(db_session, locked))["recovery_state"] == "suppressed"

    clear_maintenance_reason(locked)
    await db_session.commit()
    policy = await build_lifecycle_policy(db_session, locked)
    assert policy["recovery_state"] != "suppressed"  # no GC helper, no age gate, no residue


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_not_ready_device_is_not_suppressed(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="proj-unverified", verified=False)
    policy = await build_lifecycle_policy(db_session, device)
    assert policy["recovery_state"] != "suppressed"


async def _seed_excluded_reservation(db_session: AsyncSession, device: Device) -> None:
    await create_reserved_run(
        db_session,
        name="parity-run",
        devices=[device],
        excluded_device_ids={str(device.id)},
        exclusion_reason="excluded by test",
    )


async def _seed_maintenance_hold(db_session: AsyncSession, device: Device) -> None:
    locked = await device_locking.lock_device(db_session, device.id)
    set_maintenance_reason(locked, "operator hold")


async def _seed_deferred_stop(db_session: AsyncSession, device: Device) -> None:
    await remediation_log.append_action(
        db_session,
        device.id,
        source="device_checks",
        action=remediation_log.ACTION_AUTO_STOP_DEFERRED,
        reason="probe failed",
    )


@pytest.mark.usefixtures("seeded_driver_packs")
@pytest.mark.parametrize(
    "seed",
    [
        pytest.param(_seed_excluded_reservation, id="excluded-reservation"),
        pytest.param(_seed_maintenance_hold, id="maintenance-hold"),
        pytest.param(_seed_deferred_stop, id="deferred-stop"),
    ],
)
async def test_build_lifecycle_policy_from_facts_matches_async(
    db_session: AsyncSession,
    db_host: Host,
    seed: Callable[[AsyncSession, Device], Awaitable[None]],
) -> None:
    """The pure policy builder must return the same dict as the async wrapper."""
    device = await create_device(db_session, host_id=db_host.id, name="policy-parity")
    await seed(db_session, device)
    await db_session.commit()

    now = now_utc()
    ladder = await remediation_log.load_ladder(db_session, device.id)
    reservation_context = await run_reservation_service.get_device_reservation_with_entry(db_session, device.id)
    ready = await is_ready_for_use_async(db_session, device)
    availability = await recovery_availability(db_session, device, ready=ready, now=now)
    operational_state = await derive_operational_state(db_session, device, now=now)

    assert build_lifecycle_policy_from_facts(
        device,
        ladder=ladder,
        reservation=freeze_reservation_context(*reservation_context, device_id=device.id),
        availability=availability,
        operational_state=operational_state,
        now=now,
    ) == await build_lifecycle_policy(
        db_session,
        device,
        reservation_context=reservation_context,
        ready=ready,
        operational_state=operational_state,
        now=now,
    )
