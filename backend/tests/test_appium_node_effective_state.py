"""Phase 5: effective_state cascade for AppiumNodeRead."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from app.schemas.device import AppiumNodeRead

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host


def _build_read(**overrides: object) -> AppiumNodeRead:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "port": 4723,
        "grid_url": "http://hub:4444",
        "pid": None,
        "container_id": None,
        "active_connection_target": None,
        "state": "stopped",
        "started_at": datetime.now(UTC),
        "desired_state": "stopped",
        "desired_port": None,
        "transition_token": None,
        "transition_deadline": None,
        "last_observed_at": None,
        "health_running": None,
        "health_state": None,
        "lifecycle_policy_state": None,
    }
    base.update(overrides)
    return AppiumNodeRead.model_validate(base)


def test_effective_state_running_when_desired_running_and_pid_present() -> None:
    read = _build_read(desired_state="running", pid=12345)
    assert read.effective_state == "running"


def test_effective_state_starting_when_desired_running_but_pid_missing() -> None:
    read = _build_read(desired_state="running", pid=None)
    assert read.effective_state == "starting"


def test_effective_state_stopping_when_desired_stopped_but_pid_present() -> None:
    read = _build_read(desired_state="stopped", pid=12345)
    assert read.effective_state == "stopping"


def test_effective_state_stopped_when_desired_stopped_and_pid_none() -> None:
    read = _build_read(desired_state="stopped", pid=None)
    assert read.effective_state == "stopped"


def test_effective_state_restarting_when_active_transition_token() -> None:
    read = _build_read(
        desired_state="running",
        pid=12345,
        transition_token=uuid.uuid4(),
        transition_deadline=datetime.now(UTC) + timedelta(seconds=60),
    )
    assert read.effective_state == "restarting"


def test_effective_state_error_when_health_state_error() -> None:
    read = _build_read(desired_state="running", pid=12345, health_state="error")
    assert read.effective_state == "error"


def test_effective_state_error_when_health_running_false() -> None:
    read = _build_read(desired_state="running", pid=12345, health_running=False)
    assert read.effective_state == "error"


def test_effective_state_blocked_when_recovery_suppressed() -> None:
    read = _build_read(
        desired_state="running",
        pid=None,
        lifecycle_policy_state={
            "recovery_suppressed_reason": "Auto-manage is disabled",
            "backoff_until": None,
        },
    )
    assert read.effective_state == "blocked"


def test_effective_state_blocked_when_backoff_active() -> None:
    read = _build_read(
        desired_state="running",
        pid=None,
        lifecycle_policy_state={
            "recovery_suppressed_reason": "Node restart failed",
            "backoff_until": (datetime.now(UTC) + timedelta(seconds=120)).isoformat(),
        },
    )
    assert read.effective_state == "blocked"


def test_effective_state_not_blocked_when_backoff_expired() -> None:
    read = _build_read(
        desired_state="running",
        pid=None,
        lifecycle_policy_state={
            "recovery_suppressed_reason": "Node restart failed",
            "backoff_until": (datetime.now(UTC) - timedelta(seconds=10)).isoformat(),
        },
    )
    assert read.effective_state == "starting"


def test_effective_state_expired_transition_token_falls_through_to_running() -> None:
    read = _build_read(
        desired_state="running",
        pid=12345,
        transition_token=uuid.uuid4(),
        transition_deadline=datetime.now(UTC) - timedelta(seconds=10),
    )
    assert read.effective_state == "running"


@pytest.mark.asyncio
@pytest.mark.db
async def test_effective_state_blocked_surfaces_through_router_serialization(
    client: AsyncClient, db_session: AsyncSession, db_host: Host
) -> None:
    """Phase 6: lifecycle_policy_state must be plumbed into AppiumNodeRead so
    the blocked cascade branch fires end-to-end through the router."""
    from app.models.appium_node import AppiumNode, NodeState
    from tests.helpers import create_device

    device = await create_device(db_session, host_id=db_host.id, name="blocked-end-to-end", verified=True)
    device.lifecycle_policy_state = {
        "recovery_suppressed_reason": "Auto-manage is disabled",
        "backoff_until": None,
    }
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            desired_state=NodeState.running,
            desired_port=4723,
            pid=None,
            active_connection_target=None,
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/devices/{device.id}")
    assert resp.status_code == 200
    appium_node = resp.json()["appium_node"]
    assert appium_node["effective_state"] == "blocked"
