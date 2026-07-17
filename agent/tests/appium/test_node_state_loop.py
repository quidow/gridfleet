from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_app.appium.exceptions import PortOccupiedError, RuntimeMissingError, StartDeferredError
from agent_app.appium.node_state import NodeStateLoop


@dataclass
class _Info:
    port: int
    connection_target: str
    platform_id: str = "android_mobile"
    pid: int = 123
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class _Client:
    def __init__(self, nodes: list[dict[str, Any]]) -> None:
        self.nodes = nodes

    async def fetch_desired(self) -> dict[str, Any]:
        return {"nodes": self.nodes}


class _Manager:
    def __init__(self, running: list[_Info] | None = None, *, fail_start_with: Exception | None = None) -> None:
        self.running = running or []
        self._launch_specs: dict[int, SimpleNamespace] = {
            info.port: SimpleNamespace(
                accepting_new_sessions=True,
                stop_pending=False,
                grid_run_id=None,
            )
            for info in self.running
        }
        self.started: list[dict[str, object]] = []
        self.stopped: list[int] = []
        self.reconfigured: list[tuple[int, dict[str, Any]]] = []
        self.start_failures: list[dict[str, Any]] = []
        self._fail_start_with = fail_start_with
        self.session_active: bool | None = False
        self.session_checks: list[int] = []

    async def _node_has_active_session(self, port: int) -> bool | None:
        self.session_checks.append(port)
        return self.session_active

    def list_running(self) -> list[_Info]:
        return list(self.running)

    async def start(self, **kwargs: object) -> _Info:
        if self._fail_start_with is not None:
            raise self._fail_start_with
        self.started.append(kwargs)
        info = _Info(
            port=cast("int", kwargs["port"]),
            connection_target=cast("str", kwargs["connection_target"]),
            platform_id=cast("str", kwargs["platform_id"]),
        )
        self.running.append(info)
        self._launch_specs[info.port] = SimpleNamespace(
            accepting_new_sessions=kwargs["accepting_new_sessions"],
            stop_pending=kwargs["stop_pending"],
            grid_run_id=kwargs["grid_run_id"],
        )
        return info

    def record_start_failure(self, *, port: int, connection_target: str, kind: str, detail: str) -> None:
        self.start_failures.append(
            {"port": port, "connection_target": connection_target, "kind": kind, "detail": detail}
        )

    async def stop(self, port: int) -> None:
        self.stopped.append(port)
        self.running = [info for info in self.running if info.port != port]
        self._launch_specs.pop(port, None)

    async def reconfigure(self, port: int, **kwargs: object) -> None:
        self.reconfigured.append((port, kwargs))
        spec = self._launch_specs[port]
        for key, value in kwargs.items():
            setattr(spec, key, value)


def _node(
    *,
    desired_state: str = "running",
    port: int = 4723,
    accepting_new_sessions: bool = True,
    stop_pending: bool = False,
    grid_run_id: str | None = None,
    restart_requested_at: datetime | None = None,
    launch: dict[str, Any] | None | object = ...,  # sentinel means default launch
    unrunnable_reason: str | None = None,
) -> dict[str, Any]:
    if launch is ...:
        resolved_launch: dict[str, Any] | None = {
            "connection_target": "device-1",
            "platform_id": "android_mobile",
            "port": port,
            "pack_id": "appium-uiautomator2",
            "session_override": True,
            "accepting_new_sessions": accepting_new_sessions,
            "stop_pending": stop_pending,
            "grid_run_id": grid_run_id,
        }
    else:
        resolved_launch = launch  # type: ignore[assignment]
    return {
        "device_id": str(uuid4()),
        "desired_state": desired_state,
        "port": port,
        "accepting_new_sessions": accepting_new_sessions,
        "stop_pending": stop_pending,
        "grid_run_id": grid_run_id,
        "restart_requested_at": restart_requested_at.isoformat() if restart_requested_at else None,
        "launch": resolved_launch,
        "unrunnable_reason": unrunnable_reason,
    }


def _release_switch_launch(release: str | None) -> dict[str, Any]:
    return {
        "connection_target": "device-1",
        "platform_id": "android_mobile",
        "port": 4723,
        "pack_id": "appium-uiautomator2",
        "pack_release": release,
        "session_override": True,
        "accepting_new_sessions": True,
        "stop_pending": False,
        "grid_run_id": None,
    }


def _running_manager_with_release(release: str | None) -> _Manager:
    manager = _Manager([_Info(port=4723, connection_target="device-1")])
    manager._launch_specs[4723].pack_release = release
    return manager


@pytest.mark.asyncio
async def test_release_switch_restarts_idle_node() -> None:
    """A pack release switch reaches a running node as a drain-safe restart:
    the node restarts only once its local Appium reports zero live sessions."""
    manager = _running_manager_with_release("2026.06.0")
    manager.session_active = False
    loop = NodeStateLoop(client=_Client([_node(launch=_release_switch_launch("2026.07.0"))]), manager=manager)

    await loop.run_once()

    assert manager.stopped == [4723]
    assert len(manager.started) == 1
    assert manager.started[0]["pack_release"] == "2026.07.0"


@pytest.mark.asyncio
async def test_release_switch_waits_while_session_active() -> None:
    """A live client session blocks the release-switch restart; retry next tick."""
    manager = _running_manager_with_release("2026.06.0")
    manager.session_active = True
    loop = NodeStateLoop(client=_Client([_node(launch=_release_switch_launch("2026.07.0"))]), manager=manager)

    await loop.run_once()

    assert manager.stopped == []
    assert manager.started == []
    assert manager.session_checks == [4723]


@pytest.mark.asyncio
async def test_release_switch_waits_when_session_state_unknown() -> None:
    """Unknown session state (Appium unreachable) blocks the restart — never
    kill a possibly-live session on uncertainty."""
    manager = _running_manager_with_release("2026.06.0")
    manager.session_active = None
    loop = NodeStateLoop(client=_Client([_node(launch=_release_switch_launch("2026.07.0"))]), manager=manager)

    await loop.run_once()

    assert manager.stopped == []
    assert manager.started == []


@pytest.mark.asyncio
async def test_unversioned_launch_does_not_trigger_release_restart() -> None:
    """Legacy payloads without pack_release keep today's behavior."""
    manager = _running_manager_with_release("2026.06.0")
    loop = NodeStateLoop(client=_Client([_node(launch=_release_switch_launch(None))]), manager=manager)

    await loop.run_once()

    assert manager.stopped == []
    assert manager.started == []
    assert manager.session_checks == []


@pytest.mark.asyncio
async def test_starts_desired_running_node_that_is_not_local() -> None:
    manager = _Manager()
    loop = NodeStateLoop(client=_Client([_node()]), manager=manager)

    await loop.run_once()

    assert len(manager.started) == 1
    assert manager.started[0]["port"] == 4723
    assert manager.started[0]["connection_target"] == "device-1"


@pytest.mark.asyncio
async def test_stops_desired_stopped_node() -> None:
    manager = _Manager([_Info(port=4723, connection_target="device-1")])
    loop = NodeStateLoop(client=_Client([_node(desired_state="stopped", launch=None)]), manager=manager)

    await loop.run_once()

    assert manager.stopped == [4723]


@pytest.mark.asyncio
async def test_same_running_node_is_unchanged_until_drain_flags_differ() -> None:
    manager = _Manager([_Info(port=4723, connection_target="device-1")])
    loop = NodeStateLoop(client=_Client([_node()]), manager=manager)
    await loop.run_once()
    assert manager.started == []
    assert manager.stopped == []
    assert manager.reconfigured == []

    loop.client = _Client([_node(accepting_new_sessions=False, stop_pending=True)])
    await loop.run_once()

    assert manager.reconfigured == [
        (
            4723,
            {"accepting_new_sessions": False, "stop_pending": True, "grid_run_id": None},
        )
    ]


@pytest.mark.asyncio
async def test_stale_process_restarts_on_watermark() -> None:
    # A process spawned before the watermark is stopped and restarted exactly once:
    # the respawn carries a fresh spawn time that satisfies the same watermark.
    spawned = datetime.now(UTC) - timedelta(minutes=5)
    watermark = datetime.now(UTC) - timedelta(minutes=1)
    manager = _Manager([_Info(port=4723, connection_target="device-1", started_at=spawned)])
    loop = NodeStateLoop(client=_Client([_node(restart_requested_at=watermark)]), manager=manager)

    await loop.run_once()
    await loop.run_once()

    assert manager.stopped == [4723]
    assert len(manager.started) == 1


@pytest.mark.asyncio
async def test_fresh_process_satisfies_watermark() -> None:
    # A process spawned after the watermark is left alone — idempotent by
    # construction, no applied-token bookkeeping needed.
    watermark = datetime.now(UTC) - timedelta(minutes=1)
    spawned = datetime.now(UTC)
    manager = _Manager([_Info(port=4723, connection_target="device-1", started_at=spawned)])
    loop = NodeStateLoop(client=_Client([_node(restart_requested_at=watermark)]), manager=manager)

    await loop.run_once()

    assert manager.stopped == []
    assert manager.started == []


@pytest.mark.asyncio
async def test_no_watermark_no_restart() -> None:
    manager = _Manager([_Info(port=4723, connection_target="device-1")])
    loop = NodeStateLoop(client=_Client([_node()]), manager=manager)

    await loop.run_once()

    assert manager.stopped == []
    assert manager.started == []


@pytest.mark.asyncio
async def test_unrunnable_spec_is_not_started(caplog: pytest.LogCaptureFixture) -> None:
    manager = _Manager()
    loop = NodeStateLoop(
        client=_Client([_node(launch=None, unrunnable_reason="pack is blocked")]),
        manager=manager,
    )

    await loop.run_once()

    assert manager.started == []
    assert "pack is blocked" in caplog.text


@pytest.mark.asyncio
async def test_local_process_with_no_desired_spec_is_stopped() -> None:
    manager = _Manager([_Info(port=4799, connection_target="orphan")])
    loop = NodeStateLoop(client=_Client([]), manager=manager)

    await loop.run_once()

    assert manager.stopped == [4799]


@pytest.mark.asyncio
async def test_port_occupied_start_failure_is_recorded_as_port_conflict() -> None:
    manager = _Manager(fail_start_with=PortOccupiedError("Port 4723 is already in use"))
    loop = NodeStateLoop(client=_Client([_node()]), manager=manager)

    await loop.run_once()  # must not raise: run_once still swallows convergence failures

    assert manager.start_failures == [
        {
            "port": 4723,
            "connection_target": "device-1",
            "kind": "port_conflict",
            "detail": "Port 4723 is already in use",
        }
    ]
    assert manager.started == []


@pytest.mark.asyncio
async def test_notify_change_fires_on_start_and_stop() -> None:
    notifications: list[None] = []
    manager = _Manager([_Info(port=4723, connection_target="device-1")])
    loop = NodeStateLoop(
        client=_Client([_node(port=4723), _node(port=4724)]),
        manager=manager,
        notify_change=lambda: notifications.append(None),
    )

    # First run: port 4723 is already running and unchanged (no-op converge),
    # port 4724 must be started. Only the start should notify.
    await loop.run_once()
    assert manager.started == [manager.started[0]]  # started exactly once
    assert len(notifications) == 1

    # Desired drops port 4724: it stops via the orphan path.
    loop.client = _Client([_node(port=4723)])
    await loop.run_once()
    assert manager.stopped == [4724]
    assert len(notifications) == 2


@pytest.mark.asyncio
async def test_other_start_failure_is_recorded_as_spawn_failed() -> None:
    manager = _Manager(fail_start_with=RuntimeMissingError("appium executable not found"))
    loop = NodeStateLoop(client=_Client([_node()]), manager=manager)

    await loop.run_once()

    assert manager.start_failures == [
        {
            "port": 4723,
            "connection_target": "device-1",
            "kind": "spawn_failed",
            "detail": "appium executable not found",
        }
    ]


@pytest.mark.asyncio
async def test_boot_resolved_running_node_is_not_bounced_on_target_mismatch() -> None:
    # An emulator's launch carries a "boot" action whose connection_target is the
    # AVD name ("Pixel_6"); boot resolves it to the live adb serial ("emulator-5554")
    # and the agent runs Appium under that serial. The next convergence tick sees
    # local.connection_target ("emulator-5554") != launch.connection_target ("Pixel_6")
    # and, without this guard, treats that as a target change and stops+restarts the
    # node every tick — bouncing Appium so every session create hits a restart gap and
    # disconnects. A boot-resolved running node must be left running; a real serial
    # change (e.g. emulator console port 5554->5556) is handled by node-health/recovery,
    # and an explicit operator restart still goes through restart_requested_at.
    boot_launch = {
        "connection_target": "Pixel_6",
        "platform_id": "android_mobile",
        "port": 4728,
        "pack_id": "appium-uiautomator2",
        "session_override": True,
        "accepting_new_sessions": True,
        "stop_pending": False,
        "grid_run_id": None,
        "lifecycle_actions": [{"id": "boot"}],
    }
    manager = _Manager([_Info(port=4728, connection_target="emulator-5554")])
    loop = NodeStateLoop(client=_Client([_node(port=4728, launch=boot_launch)]), manager=manager)

    await loop.run_once()

    # The running, boot-resolved node is untouched: no stop, no restart.
    assert manager.stopped == []
    assert manager.started == []


@pytest.mark.asyncio
async def test_target_change_still_restarts_node_without_boot_action() -> None:
    # A real device's launch has no "boot" action: its connection_target is the
    # direct serial, and a genuine target change must still stop+restart the node.
    # Guards against over-broadly skipping target_changed restarts.
    real_launch = {
        "connection_target": "new-serial",
        "platform_id": "android_mobile",
        "port": 4723,
        "pack_id": "appium-uiautomator2",
        "session_override": True,
        "accepting_new_sessions": True,
        "stop_pending": False,
        "grid_run_id": None,
        "lifecycle_actions": [],
    }
    manager = _Manager([_Info(port=4723, connection_target="old-serial")])
    loop = NodeStateLoop(client=_Client([_node(port=4723, launch=real_launch)]), manager=manager)

    await loop.run_once()

    # A real target change (no boot resolution) still restarts the node.
    assert manager.stopped == [4723]
    assert len(manager.started) == 1
    assert manager.started[0]["connection_target"] == "new-serial"


@pytest.mark.asyncio
async def test_deferred_start_is_not_recorded_as_failure(caplog: pytest.LogCaptureFixture) -> None:
    # A boot that has not yet resolved a device serial defers the start rather than
    # spawning Appium with an unresolved udid. Deferral is transient (emulator still
    # booting / adb momentarily unresponsive), so it must NOT be recorded as a
    # start_failure — that would feed the backend's recovery/review escalation and
    # trade the session-viability backoff spiral for a start-failure spiral.
    manager = _Manager(fail_start_with=StartDeferredError("boot for 'Pixel_6' not resolved yet"))
    loop = NodeStateLoop(client=_Client([_node()]), manager=manager)

    with caplog.at_level(logging.INFO, logger="agent_app.appium.node_state"):
        await loop.run_once()

    assert manager.start_failures == []
    assert manager.started == []
    assert "not resolved yet" in caplog.text
