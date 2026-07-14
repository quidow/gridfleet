import ast
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import select

from app.devices.services.connectivity import ConnectivityService
from app.devices.services.health import DeviceHealthService
from app.jobs.models import Job
from tests.fakes import FakeSettingsReader
from tests.helpers import seed_host_and_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


CONNECTIVITY_SOURCE = Path(__file__).parents[2] / "app/devices/services/connectivity.py"

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def test_no_inline_repair_dispatch_symbols() -> None:
    source = CONNECTIVITY_SOURCE.read_text()
    tree = ast.parse(source)
    method_names = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    assert "_maybe_dispatch_repair" not in method_names
    assert "_reprobe_after_repair" not in method_names
    assert "dispatch_recommended_action" not in source


async def test_present_unhealthy_device_enqueues_remediation_without_inline_dispatch(
    db_session: AsyncSession,
) -> None:
    host, device = await seed_host_and_device(db_session, identity="remediation-split")
    unhealthy = {
        "healthy": False,
        "checks": [{"check_id": "adb_connected", "ok": False}],
        "recommended_action": "reconnect",
    }
    dispatch = AsyncMock()

    with (
        patch("app.devices.services.connectivity._lifecycle_state_capable", new=AsyncMock(return_value=False)),
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new=AsyncMock(return_value={device.identity_value}),
        ),
        patch("app.devices.services.link_repair.dispatch_recommended_action", new=dispatch),
    ):
        await ConnectivityService(
            publisher=Mock(),
            settings=FakeSettingsReader(),
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=DeviceHealthService(publisher=Mock()),
        ).fold_host_device_health(
            db_session,
            host.id,
            {
                "devices": {device.connection_target: unhealthy},
            },
        )

    jobs = (await db_session.execute(select(Job).where(Job.remediation_device_id == device.id))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].failure_episode_id == device.failure_episode_id
    assert jobs[0].remediation_action_id == "reconnect"
    dispatch.assert_not_awaited()


async def test_remediation_is_persisted_before_lifecycle_policy_commit_and_crash(
    db_session: AsyncSession,
) -> None:
    host, device = await seed_host_and_device(db_session, identity="remediation-atomic")
    unhealthy = {
        "healthy": False,
        "checks": [{"check_id": "adb_connected", "ok": False}],
        "recommended_action": "reconnect",
    }
    lifecycle_policy = AsyncMock()

    async def commit_then_crash(db: AsyncSession, *_args: object, **_kwargs: object) -> None:
        await db.commit()
        raise RuntimeError("lifecycle policy crashed after commit")

    lifecycle_policy.handle_health_failure.side_effect = commit_then_crash
    with (
        patch("app.devices.services.connectivity._lifecycle_state_capable", new=AsyncMock(return_value=False)),
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new=AsyncMock(return_value={device.identity_value}),
        ),
        pytest.raises(RuntimeError, match="lifecycle policy crashed after commit"),
    ):
        await ConnectivityService(
            publisher=Mock(),
            settings=FakeSettingsReader(),
            circuit_breaker=Mock(),
            lifecycle_policy=lifecycle_policy,
            health=DeviceHealthService(publisher=Mock()),
        ).fold_host_device_health(
            db_session,
            host.id,
            {
                "devices": {device.connection_target: unhealthy},
            },
        )

    await db_session.refresh(device)
    jobs = (await db_session.execute(select(Job).where(Job.remediation_device_id == device.id))).scalars().all()
    assert device.failure_episode_id is not None
    assert len(jobs) == 1
    assert jobs[0].failure_episode_id == device.failure_episode_id
