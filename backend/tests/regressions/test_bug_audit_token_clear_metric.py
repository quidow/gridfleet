"""Bug 4: ``_clear_transition_token`` emits the override metric on natural deadline expiry.

See ``docs/superpowers/specs/2026-05-20-backend-bug-audit.md#bug-4``.

When the reconciler clears an expired transition token it calls
``write_desired_state`` with ``transition_token=None`` (default).
``write_desired_state`` then enters the "old_token is not None and
old_token != transition_token" branch at
``desired_state_writer.py:83-96`` and increments
``APPIUM_TRANSITION_TOKEN_OVERRIDDEN`` — but no operator override
happened; the deadline simply elapsed. The metric should fire only on
genuine override (one writer beats another); deadline expiry is a
distinct event that should not pollute the override counter.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.desired_state_writer import write_desired_state
from app.core.metrics_recorders import APPIUM_TRANSITION_TOKEN_OVERRIDDEN
from app.devices.models import DeviceOperationalState
from tests.helpers import create_device, create_host

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession


def _override_total() -> float:
    return sum(
        sample.value
        for metric in APPIUM_TRANSITION_TOKEN_OVERRIDDEN.collect()
        for sample in metric.samples
        if sample.name.endswith("_total") and sample.labels.get("winning_source") == "appium_reconciler"
    )


@pytest.mark.db
@pytest.mark.asyncio
async def test_clear_expired_transition_token_does_not_emit_override_metric(
    db_session: AsyncSession,
    client: AsyncClient,
) -> None:
    host = await create_host(client)
    device = await create_device(
        db_session,
        host_id=uuid.UUID(host["id"]),
        name="token-clear",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    expired_token = uuid.uuid4()
    from app.devices.services import state_write_guard

    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            transition_token=expired_token,
            transition_deadline=datetime.now(UTC) - timedelta(seconds=30),
        )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(node)

    before = _override_total()

    # Mirror ``reconciler._clear_transition_token``: clear by writing the
    # current desired state with the natural-clear flag set.
    await write_desired_state(
        db_session,
        node=node,
        target=node.desired_state,
        caller="appium_reconciler",
        desired_port=node.desired_port,
        transition_token_natural_clear=True,
    )
    await db_session.commit()

    after = _override_total()

    # Fixed behavior: the writer recognises this as a natural-expiry clear
    # (no new token contended for the slot) and skips the override metric.
    # Current behavior (bug): the metric ticks on every deadline-expiry sweep.
    assert after == before, (
        f"APPIUM_TRANSITION_TOKEN_OVERRIDDEN incremented on natural expiry (before={before}, after={after})"
    )
