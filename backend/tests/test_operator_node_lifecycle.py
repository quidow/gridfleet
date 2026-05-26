"""Regression tests for unified operator-driven Appium node lifecycle writes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import DeviceIntent
from app.devices.services import state_write_guard
from app.devices.services.intent_reconciler import _reconcile_expired_intents, reconcile_device
from app.devices.services.operator_node_lifecycle import request_restart
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _FrozenDatetime:
    """Drop-in replacement for the ``datetime`` class that freezes ``now()``."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self, tz: timezone | None = None) -> datetime:
        return self._now if tz is None else self._now.astimezone(tz)

    def __getattr__(self, name: str) -> object:
        return getattr(datetime, name)


class _FakeDevice:
    """Minimal device stub — only ``id`` is required by the helpers under test."""

    def __init__(self, device_id: uuid.UUID) -> None:
        self.id = device_id


class _FakeSettings:
    """Stub for ``settings_service`` — returns hard-coded values for known keys."""

    def get(self, key: str) -> object:
        if key == "appium_reconciler.restart_window_sec":
            return 120
        raise KeyError(key)


async def test_stale_operator_start_intent_does_not_force_old_desired_port(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Pre-PR-#301-shape stale operator:start intent must not overwrite a freshly
    allocated desired_port. Repro for the Roku flip observed on 2026-05-18:
    stale payload re-asserted desired_port=4724 every intent_reconciler tick,
    while start_node had just allocated port 4725.
    """
    device = await create_device(db_session, host_id=db_host.id, name="roku-flip-repro", verified=True)
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4725,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4725,
            pid=27765,
            active_connection_target=device.connection_target,
        )
    db_session.add(node)
    await db_session.flush()
    device.appium_node = node

    stale_token = uuid.uuid4()
    stale_deadline = datetime.now(UTC) - timedelta(days=2)
    stale_intent = DeviceIntent(
        device_id=device.id,
        source=f"operator:start:{device.id}",
        axis="node_process",
        payload={
            "action": "start",
            "priority": 20,
            "desired_port": 4724,
            "transition_token": str(stale_token),
            "transition_deadline": stale_deadline.isoformat(),
        },
        precondition=None,  # pre-#301 row shape
        expires_at=None,
        created_at=stale_deadline - timedelta(minutes=2),
        updated_at=stale_deadline - timedelta(minutes=2),
    )
    db_session.add(stale_intent)
    await db_session.commit()

    # Run reconcile WITHOUT any operator action — the stale intent must continue
    # to mis-assert desired_port=4724 because the precondition/expires_at sweeps
    # both skip rows where those columns are NULL. This documents the bug class
    # that pre-#301 stale rows fall into.
    await reconcile_device(db_session, device.id)
    await db_session.refresh(node)
    assert node.desired_port == 4724, (
        "pre-fix sanity check: the stale pre-#301 row should still mis-assert "
        "desired_port=4724 before any operator action refreshes the intent"
    )

    # Now simulate an operator pressing Restart through the unified path. This
    # upserts the operator:start intent with a fresh transition_token,
    # transition_deadline, expires_at, AND precondition — overwriting the stale
    # payload. Reconcile inside register_intents_and_reconcile then writes the
    # AppiumNode desired_port from the fresh payload (= node.port = 4725).
    await request_restart(db_session, device, caller="operator_restart", reason="operator restart")
    await db_session.refresh(node)

    assert node.desired_port == 4725, (
        f"after a unified-path restart, desired_port should match the running port; got {node.desired_port}"
    )

    intent = (
        await db_session.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.source == f"operator:start:{device.id}",
            )
        )
    ).scalar_one()
    assert intent.payload.get("transition_token") != str(stale_token), (
        "stale transition_token must be replaced by the fresh restart"
    )
    assert intent.expires_at is not None, "fresh restart must set expires_at"
    assert intent.expires_at > datetime.now(UTC), "fresh expires_at must be in the future"


def test_operator_restart_intent_sets_expires_at_and_preserves_precondition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """operator_restart_intent must set expires_at = now + window_sec, embed the
    same deadline in the payload, and preserve the PR #301 node_running precondition.
    """
    from app.devices.services import operator_node_lifecycle as mod
    from app.devices.services.operator_node_lifecycle import operator_restart_intent

    fixed_now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(mod, "datetime", _FrozenDatetime(fixed_now))
    monkeypatch.setattr(mod, "_default_settings", _FakeSettings())

    device_id = uuid.uuid4()
    device = _FakeDevice(device_id)

    intent = operator_restart_intent(device, desired_port=4725)  # type: ignore[arg-type]

    expected_deadline = fixed_now + timedelta(seconds=120)

    assert intent.expires_at is not None
    assert intent.expires_at == expected_deadline
    assert intent.payload["transition_deadline"] == expected_deadline.isoformat()
    assert intent.precondition == {
        "kind": "node_running",
        "device_id": str(device_id),
        "expected": False,
    }


async def test_reconcile_expired_intents_deletes_expired_restart_intent(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """_reconcile_expired_intents must delete DeviceIntent rows whose expires_at
    has passed, even when expires_at is explicitly set (as opposed to the Task 1
    regression where expires_at was NULL).
    """
    device = await create_device(db_session, host_id=db_host.id, name="gc-expired-restart", verified=True)

    expired_intent = DeviceIntent(
        device_id=device.id,
        source=f"operator:start:{device.id}",
        axis="node_process",
        payload={
            "action": "start",
            "priority": 20,
            "desired_port": 4725,
            "transition_token": str(uuid.uuid4()),
            "transition_deadline": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        },
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
        created_at=datetime.now(UTC) - timedelta(minutes=10),
        updated_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    db_session.add(expired_intent)
    await db_session.commit()

    await _reconcile_expired_intents(db_session)
    await db_session.commit()

    remaining = (
        (await db_session.execute(select(DeviceIntent).where(DeviceIntent.device_id == device.id))).scalars().all()
    )
    assert remaining == [], (
        f"expected no intents after GC sweep, found {len(remaining)}: {[r.source for r in remaining]}"
    )


async def test_two_consecutive_request_restarts_refresh_intent_payload(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Each operator restart must produce a fresh transition_token + expires_at.

    Pre-PR-#301, a stale operator:start intent payload could re-assert old
    transition_token/desired_port indefinitely. The unified path overwrites the
    full payload on every restart.
    """
    device = await create_device(db_session, host_id=db_host.id, name="rr-refresh", verified=True)
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4725,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4725,
            pid=27765,
            active_connection_target=device.connection_target,
        )
    db_session.add(node)
    await db_session.flush()
    device.appium_node = node
    # observed_running on AppiumNode is a hybrid/derived flag; verify the
    # fixture is constructed so the model treats the node as running.
    assert node.observed_running, "test fixture must seed an observed-running node"

    await request_restart(db_session, device, caller="operator_restart", reason="first")
    intent_first = (
        await db_session.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.source == f"operator:start:{device.id}",
            )
        )
    ).scalar_one()
    first_token = intent_first.payload["transition_token"]
    first_deadline = intent_first.expires_at

    await request_restart(db_session, device, caller="operator_restart", reason="second")
    # Use populate_existing so the query bypasses the SQLAlchemy identity-map
    # cache and reloads the upserted payload from the DB.
    intent_second = (
        await db_session.execute(
            select(DeviceIntent)
            .where(
                DeviceIntent.device_id == device.id,
                DeviceIntent.source == f"operator:start:{device.id}",
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    assert intent_second.payload["transition_token"] != first_token, "transition_token must rotate on each restart"
    assert intent_second.expires_at is not None
    assert first_deadline is not None
    assert intent_second.expires_at > first_deadline, "expires_at must move forward on each restart"
