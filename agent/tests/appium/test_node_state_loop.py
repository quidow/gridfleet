from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_app.appium.node_state import NodeStateLoop


@dataclass
class _Info:
    port: int
    connection_target: str
    platform_id: str = "android_mobile"
    pid: int = 123


class _Client:
    def __init__(self, nodes: list[dict[str, Any]]) -> None:
        self.nodes = nodes

    async def fetch_desired(self) -> dict[str, Any]:
        return {"nodes": self.nodes, "generation_hint": max((node["generation"] for node in self.nodes), default=0)}


class _Manager:
    def __init__(self, running: list[_Info] | None = None) -> None:
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

    def list_running(self) -> list[_Info]:
        return list(self.running)

    async def start(self, **kwargs: object) -> _Info:
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
    generation: int = 1,
    accepting_new_sessions: bool = True,
    stop_pending: bool = False,
    grid_run_id: str | None = None,
    transition_token: str | None = None,
    transition_deadline: datetime | None = None,
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
        "generation": generation,
        "desired_state": desired_state,
        "port": port,
        "accepting_new_sessions": accepting_new_sessions,
        "stop_pending": stop_pending,
        "grid_run_id": grid_run_id,
        "transition_token": transition_token,
        "transition_deadline": transition_deadline.isoformat() if transition_deadline else None,
        "launch": resolved_launch,
        "unrunnable_reason": unrunnable_reason,
    }


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
async def test_unexpired_transition_token_restarts_only_once() -> None:
    manager = _Manager([_Info(port=4723, connection_target="device-1")])
    token = str(uuid4())
    node = _node(
        transition_token=token,
        transition_deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    loop = NodeStateLoop(client=_Client([node]), manager=manager)

    await loop.run_once()
    await loop.run_once()

    assert manager.stopped == [4723]
    assert len(manager.started) == 1
    assert token in loop.applied_tokens


@pytest.mark.asyncio
async def test_expired_transition_token_does_not_restart() -> None:
    manager = _Manager([_Info(port=4723, connection_target="device-1")])
    loop = NodeStateLoop(
        client=_Client(
            [
                _node(
                    transition_token=str(uuid4()),
                    transition_deadline=datetime.now(UTC) - timedelta(seconds=1),
                )
            ]
        ),
        manager=manager,
    )

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
