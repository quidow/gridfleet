from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host
from app.lifecycle.services.remediation_log import (
    ACTION_AUTO_STOP_CLEARED,
    ACTION_AUTO_STOP_COMMISSIONED,
    ACTION_AUTO_STOP_DEFERRED,
    ACTION_RECOVERY_STARTED,
    ACTION_RESTART_COMMISSIONED,
    KIND_ACTION,
    KIND_FAILURE,
    KIND_RESET,
    advance_ladder,
    append_action,
    append_entry,
    derive_ladder,
)
from tests.helpers import create_device_record

pytestmark = pytest.mark.db


@pytest.mark.parametrize(
    ("kind", "action"),
    [
        (KIND_FAILURE, "failure_observed"),
        (KIND_ACTION, ACTION_AUTO_STOP_DEFERRED),
        (KIND_ACTION, ACTION_AUTO_STOP_CLEARED),
        (KIND_ACTION, ACTION_AUTO_STOP_COMMISSIONED),
        (KIND_ACTION, ACTION_RESTART_COMMISSIONED),
        (KIND_ACTION, ACTION_RECOVERY_STARTED),
        (KIND_RESET, "self_healed"),
    ],
)
async def test_advance_ladder_matches_full_derivation(
    db_session: AsyncSession,
    db_host: Host,
    kind: str,
    action: str,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value=f"dev-{kind}-{action}",
        name="dev",
    )
    prior_entry = await append_action(
        db_session,
        device.id,
        source="test",
        action=ACTION_RESTART_COMMISSIONED,
        reason="prior",
    )
    prior = derive_ladder([prior_entry])
    entry = await append_entry(
        db_session,
        device.id,
        kind=kind,
        source="test",
        action=action,
        reason="next",
    )

    assert advance_ladder(prior, entry) == derive_ladder([prior_entry, entry])


async def test_advance_ladder_preserves_restart_watermark_behind_stop(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="dev-watermark",
        name="dev",
    )
    restart = await append_action(
        db_session,
        device.id,
        source="node_health",
        action=ACTION_RESTART_COMMISSIONED,
        reason="restart",
    )
    stop = await append_action(
        db_session,
        device.id,
        source="device_checks",
        action=ACTION_AUTO_STOP_COMMISSIONED,
        reason="stop",
    )
    recovery = await append_action(
        db_session,
        device.id,
        source="device_checks",
        action=ACTION_RECOVERY_STARTED,
        reason="recover",
    )
    prior = derive_ladder([restart, stop])
    assert prior.node_directive is not None and prior.node_directive.restart_watermark is None
    assert prior.last_restart_at == restart.at
    assert advance_ladder(prior, recovery) == derive_ladder([restart, stop, recovery])
    assert derive_ladder([restart, stop, recovery]).node_directive.restart_watermark == restart.at
