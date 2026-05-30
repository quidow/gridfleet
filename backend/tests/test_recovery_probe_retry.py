from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SettingsReader
    from app.devices.models import Device
    from app.events.protocols import EventPublisher
    from app.hosts.models import Host


@pytest.mark.asyncio
async def test_recovery_probe_stops_on_first_success() -> None:
    from app.devices.services import lifecycle_policy

    probe_mock = AsyncMock(return_value={"status": "passed"})

    with (
        patch.object(lifecycle_policy.session_viability, "run_session_viability_probe", probe_mock),
        patch.object(lifecycle_policy, "_reload_device", AsyncMock(side_effect=lambda db, dev: dev)),
    ):
        result = await lifecycle_policy._run_recovery_probe(
            SimpleNamespace(), SimpleNamespace(id="dev-1"), settings=FakeSettingsReader({}), publisher=event_bus
        )

    assert probe_mock.await_count == 1
    assert result == {"status": "passed"}


@pytest.mark.asyncio
async def test_recovery_probe_retries_until_attempts_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.devices.services import lifecycle_policy

    monkeypatch.setattr(lifecycle_policy, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(lifecycle_policy, "RECOVERY_PROBE_JITTER_MAX_SEC", 0)

    probe_mock = AsyncMock(return_value={"status": "failed", "error": "boom"})

    with (
        patch.object(lifecycle_policy.session_viability, "run_session_viability_probe", probe_mock),
        patch.object(lifecycle_policy, "_reload_device", AsyncMock(side_effect=lambda db, dev: dev)),
    ):
        result = await lifecycle_policy._run_recovery_probe(
            SimpleNamespace(), SimpleNamespace(id="dev-1"), settings=FakeSettingsReader({}), publisher=event_bus
        )

    assert probe_mock.await_count == lifecycle_policy.RECOVERY_PROBE_ATTEMPTS
    assert result == {"status": "failed", "error": "boom"}


@pytest.mark.asyncio
async def test_recovery_probe_retries_then_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.devices.services import lifecycle_policy

    monkeypatch.setattr(lifecycle_policy, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(lifecycle_policy, "RECOVERY_PROBE_JITTER_MAX_SEC", 0)

    outcomes: list[dict[str, Any]] = [
        {"status": "failed", "error": "x"},
        {"status": "failed", "error": "y"},
        {"status": "passed"},
    ]
    probe_mock = AsyncMock(side_effect=outcomes)

    with (
        patch.object(lifecycle_policy.session_viability, "run_session_viability_probe", probe_mock),
        patch.object(lifecycle_policy, "_reload_device", AsyncMock(side_effect=lambda db, dev: dev)),
    ):
        result = await lifecycle_policy._run_recovery_probe(
            SimpleNamespace(), SimpleNamespace(id="dev-1"), settings=FakeSettingsReader({}), publisher=event_bus
        )

    assert probe_mock.await_count == 3
    assert result == {"status": "passed"}


@pytest.mark.asyncio
async def test_recovery_probe_forwards_publisher_to_session_viability() -> None:
    """The recovery probe must thread ``publisher`` through to the session
    viability probe. ``run_session_viability_probe`` drives the device through
    ``_MACHINE.transition`` → ``set_operational_state``, which asserts
    ``publisher is not None`` when ``publish_event=True``. A missing forward
    here crashes the connectivity loop's recovery path every tick and wedges
    offline devices permanently (no recovery probe can ever complete)."""
    from app.devices.services import lifecycle_policy

    publisher = AsyncMock()
    probe_mock = AsyncMock(return_value={"status": "passed"})

    with (
        patch.object(lifecycle_policy.session_viability, "run_session_viability_probe", probe_mock),
        patch.object(lifecycle_policy, "_reload_device", AsyncMock(side_effect=lambda db, dev: dev)),
    ):
        await lifecycle_policy._run_recovery_probe(
            SimpleNamespace(),
            SimpleNamespace(id="dev-1"),
            settings=FakeSettingsReader({}),
            publisher=publisher,
        )

    assert probe_mock.await_args.kwargs.get("publisher") is publisher


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_attempt_auto_recovery_forwards_publisher_to_recovery_probe(
    db_session: AsyncSession, db_host: Host
) -> None:
    """``attempt_auto_recovery`` accepts a publisher from its caller (the
    connectivity loop) and must forward it to ``_run_recovery_probe`` so the
    inner session-viability state transitions can emit events. A missing
    forward here surfaces as an ``AssertionError`` from ``set_operational_state``
    on every connectivity tick once a device goes offline — the recovery probe
    never lands and the device stays wedged in ``offline``."""
    from app.appium_nodes.models import AppiumDesiredState, AppiumNode
    from app.devices.services import lifecycle_policy, state_write_guard
    from app.events.protocols import EventPublisher
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
    captured: dict[str, EventPublisher | None] = {}

    async def _capture(
        db: AsyncSession,
        dev: Device,
        *,
        settings: SettingsReader,
        publisher: EventPublisher | None = None,
    ) -> dict[str, Any]:
        captured["publisher"] = publisher
        return {"status": "passed"}

    from app.devices.services.lifecycle_policy import LifecyclePolicyService
    from app.devices.services.lifecycle_policy_actions import LifecyclePolicyActionsService

    svc = LifecyclePolicyService(
        publisher=publisher,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(publisher=publisher),
        viability=Mock(),
    )
    with patch.object(lifecycle_policy, "_run_recovery_probe", new=_capture):
        await svc.attempt_auto_recovery(
            db_session,
            device,
            source="connectivity",
            reason="test",
        )

    assert captured.get("publisher") is publisher
