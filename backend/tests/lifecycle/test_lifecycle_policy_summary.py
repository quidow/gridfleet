from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.devices import locking as device_locking
from app.devices.services.lifecycle_policy_state import (
    MAINTENANCE_HOLD_SUPPRESSION_REASON,
    clear_maintenance_reason,
    set_maintenance_reason,
)
from app.devices.services.lifecycle_policy_summary import build_lifecycle_policy, build_lifecycle_policy_summary
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


def test_maintenance_summary_uses_maintenance_reason_instead_of_tautology() -> None:
    policy = {
        "recovery_state": "suppressed",
        "recovery_suppressed_reason": "Device is in maintenance mode",
        "maintenance_reason": "Cooldown escalation",
        "last_failure_reason": None,
        "last_failure_source": None,
        "last_action": None,
        "stop_pending": False,
        "stop_pending_reason": None,
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
        "stop_pending": False,
        "stop_pending_reason": None,
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
        "stop_pending": False,
        "stop_pending_reason": None,
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
