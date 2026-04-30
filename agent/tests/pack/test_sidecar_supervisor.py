"""Tests for ``agent_app.pack.sidecar_supervisor.SidecarSupervisor``.

A fake adapter records each ``sidecar_lifecycle`` call and returns a scripted
``SidecarStatus``. The supervisor is exercised end-to-end with very short poll
intervals so time-based behaviour can be asserted under one second per case.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Literal

import pytest

from agent_app.pack.adapter_dispatch import AdapterHookExecutionError
from agent_app.pack.adapter_types import SidecarStatus
from agent_app.pack.sidecar_supervisor import SidecarHandle, SidecarSupervisor


class _ScriptedAdapter:
    """Fake adapter whose sidecar_lifecycle returns scripted statuses per action.

    ``scripts[action]`` is a deque of statuses. Each call to ``sidecar_lifecycle``
    pops the next status. If the deque is empty, the last seen status (or a
    default) is returned. ``calls`` records every (action, feature_id) tuple.
    """

    pack_id = "vendor-fake"
    pack_release = "1.0.0"

    def __init__(
        self,
        *,
        starts: list[SidecarStatus] | None = None,
        statuses: list[SidecarStatus] | None = None,
        stops: list[SidecarStatus] | None = None,
    ) -> None:
        self._scripts: dict[str, deque[SidecarStatus]] = {
            "start": deque(starts or [SidecarStatus(ok=True, detail="started", state="running")]),
            "status": deque(statuses or [SidecarStatus(ok=True, detail="alive", state="running")]),
            "stop": deque(stops or [SidecarStatus(ok=True, detail="stopped", state="stopped")]),
        }
        self._last: dict[str, SidecarStatus] = {}
        self.calls: list[tuple[str, str]] = []

    async def sidecar_lifecycle(
        self,
        feature_id: str,
        action: Literal["start", "stop", "status"],
    ) -> SidecarStatus:
        self.calls.append((action, feature_id))
        script = self._scripts[action]
        if script:
            self._last[action] = script.popleft()
        return self._last.get(action, SidecarStatus(ok=True, detail="", state=""))

    # The supervisor only ever calls sidecar_lifecycle, but the protocol requires
    # the rest. Provide stubs to satisfy the type-checker via duck typing.
    async def discover(self, ctx: object) -> list[object]:  # pragma: no cover - unused
        return []

    async def doctor(self, ctx: object) -> list[object]:  # pragma: no cover - unused
        return []

    async def health_check(self, ctx: object) -> list[object]:  # pragma: no cover - unused
        return []

    async def lifecycle_action(
        self, action_id: object, args: object, ctx: object
    ) -> object:  # pragma: no cover - unused
        return None

    async def pre_session(self, spec: object) -> dict[str, object]:  # pragma: no cover - unused
        return {}

    async def post_session(self, spec: object, outcome: object) -> None:  # pragma: no cover - unused
        return None

    async def feature_action(
        self, feature_id: str, action_id: str, args: object, ctx: object
    ) -> object:  # pragma: no cover - unused
        return None


class _RaisingAdapter:
    """Fake adapter whose start hook raises a RuntimeError."""

    pack_id = "vendor-bad"
    pack_release = "1.0.0"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def sidecar_lifecycle(
        self,
        feature_id: str,
        action: Literal["start", "stop", "status"],
    ) -> SidecarStatus:
        self.calls.append((action, feature_id))
        raise RuntimeError("sidecar boom")

    async def discover(self, ctx: object) -> list[object]:  # pragma: no cover - unused
        return []

    async def doctor(self, ctx: object) -> list[object]:  # pragma: no cover - unused
        return []

    async def health_check(self, ctx: object) -> list[object]:  # pragma: no cover - unused
        return []

    async def lifecycle_action(
        self, action_id: object, args: object, ctx: object
    ) -> object:  # pragma: no cover - unused
        return None

    async def pre_session(self, spec: object) -> dict[str, object]:  # pragma: no cover - unused
        return {}

    async def post_session(self, spec: object, outcome: object) -> None:  # pragma: no cover - unused
        return None

    async def feature_action(
        self, feature_id: str, action_id: str, args: object, ctx: object
    ) -> object:  # pragma: no cover - unused
        return None


# ---------------------------------------------------------------------------
# start / stop / snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_calls_adapter_and_returns_status() -> None:
    adapter = _ScriptedAdapter()
    sup = SidecarSupervisor(poll_interval_seconds=0.05)
    try:
        status = await sup.start(
            pack_id="vendor-fake",
            release="1.0.0",
            feature_id="tunnel",
            adapter=adapter,  # type: ignore[arg-type]
        )
        assert status.ok is True
        assert status.detail == "started"
        assert ("start", "tunnel") in adapter.calls
        # handle stored
        snap = sup.status_snapshot()
        assert len(snap) == 1
        assert snap[0]["pack_id"] == "vendor-fake"
        assert snap[0]["release"] == "1.0.0"
        assert snap[0]["feature_id"] == "tunnel"
        assert snap[0]["ok"] is True
    finally:
        await sup.shutdown()


@pytest.mark.asyncio
async def test_start_when_adapter_returns_not_ok_does_not_schedule_poll() -> None:
    adapter = _ScriptedAdapter(
        starts=[SidecarStatus(ok=False, detail="port in use", state="failed")],
    )
    sup = SidecarSupervisor(poll_interval_seconds=0.05)
    try:
        status = await sup.start(
            pack_id="vendor-fake",
            release="1.0.0",
            feature_id="tunnel",
            adapter=adapter,  # type: ignore[arg-type]
        )
        assert status.ok is False
        assert status.detail == "port in use"
        # Wait long enough that any poll task would have fired multiple times
        await asyncio.sleep(0.2)
        # adapter must have only been hit for "start" — no "status" calls
        actions = [a for a, _ in adapter.calls]
        assert actions == ["start"]
        # handle stored, no poll task running
        handle = sup._handles[("vendor-fake", "1.0.0", "tunnel")]  # type: ignore[attr-defined]
        assert handle.poll_task is None
        assert handle.last_status.ok is False
    finally:
        await sup.shutdown()


@pytest.mark.asyncio
async def test_start_idempotent_for_running_handle() -> None:
    adapter = _ScriptedAdapter()
    sup = SidecarSupervisor(poll_interval_seconds=10.0)  # long → no polls during this test
    try:
        first = await sup.start(
            pack_id="vendor-fake",
            release="1.0.0",
            feature_id="tunnel",
            adapter=adapter,  # type: ignore[arg-type]
        )
        # second call should not re-invoke adapter.start
        second = await sup.start(
            pack_id="vendor-fake",
            release="1.0.0",
            feature_id="tunnel",
            adapter=adapter,  # type: ignore[arg-type]
        )
        assert first.ok is True
        assert second.ok is True
        # only ONE start call recorded
        assert [a for a, _ in adapter.calls].count("start") == 1
    finally:
        await sup.shutdown()


@pytest.mark.asyncio
async def test_stop_cancels_poll_task_and_calls_adapter_stop() -> None:
    adapter = _ScriptedAdapter()
    sup = SidecarSupervisor(poll_interval_seconds=0.05)
    try:
        await sup.start(
            pack_id="vendor-fake",
            release="1.0.0",
            feature_id="tunnel",
            adapter=adapter,  # type: ignore[arg-type]
        )
        # confirm poll task exists and is active
        handle_before = sup._handles[("vendor-fake", "1.0.0", "tunnel")]  # type: ignore[attr-defined]
        assert handle_before.poll_task is not None
        assert not handle_before.poll_task.done()
        poll_task = handle_before.poll_task

        stop_status = await sup.stop(
            pack_id="vendor-fake",
            release="1.0.0",
            feature_id="tunnel",
            adapter=adapter,  # type: ignore[arg-type]
        )
        assert stop_status.detail == "stopped"
        # adapter "stop" called
        assert ("stop", "tunnel") in adapter.calls
        # poll task cancelled
        assert poll_task.cancelled() or poll_task.done()
        # handle removed
        assert ("vendor-fake", "1.0.0", "tunnel") not in sup._handles  # type: ignore[attr-defined]
    finally:
        await sup.shutdown()


@pytest.mark.asyncio
async def test_status_snapshot_returns_all_handles() -> None:
    adapter_a = _ScriptedAdapter()
    adapter_b = _ScriptedAdapter(
        starts=[SidecarStatus(ok=False, detail="bad", state="failed")],
    )
    sup = SidecarSupervisor(poll_interval_seconds=10.0)
    try:
        await sup.start(
            pack_id="vendor-fake",
            release="1.0.0",
            feature_id="tunnel",
            adapter=adapter_a,  # type: ignore[arg-type]
        )
        await sup.start(
            pack_id="vendor-fake",
            release="1.0.0",
            feature_id="bridge",
            adapter=adapter_b,  # type: ignore[arg-type]
        )
        snap = sup.status_snapshot()
        feature_ids = sorted(entry["feature_id"] for entry in snap)
        assert feature_ids == ["bridge", "tunnel"]
        by_id = {entry["feature_id"]: entry for entry in snap}
        assert by_id["tunnel"]["ok"] is True
        assert by_id["bridge"]["ok"] is False
        assert by_id["bridge"]["detail"] == "bad"
    finally:
        await sup.shutdown()


@pytest.mark.asyncio
async def test_tracked_keys_reports_started_sidecars() -> None:
    adapter = _ScriptedAdapter()
    sup = SidecarSupervisor(poll_interval_seconds=10.0)
    try:
        await sup.start(
            pack_id="vendor-fake",
            release="1.0.0",
            feature_id="tunnel",
            adapter=adapter,  # type: ignore[arg-type]
        )

        assert sup.tracked_keys() == {("vendor-fake", "1.0.0", "tunnel")}
    finally:
        await sup.shutdown()


@pytest.mark.asyncio
async def test_drop_removes_handle_without_adapter_stop_call() -> None:
    adapter = _ScriptedAdapter()
    sup = SidecarSupervisor(poll_interval_seconds=10.0)
    try:
        await sup.start(
            pack_id="vendor-fake",
            release="1.0.0",
            feature_id="tunnel",
            adapter=adapter,  # type: ignore[arg-type]
        )

        await sup.drop(pack_id="vendor-fake", release="1.0.0", feature_id="tunnel")

        assert sup.tracked_keys() == set()
        assert ("stop", "tunnel") not in adapter.calls
    finally:
        await sup.shutdown()


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_loop_updates_status_periodically() -> None:
    adapter = _ScriptedAdapter(
        starts=[SidecarStatus(ok=True, detail="started", state="running")],
        statuses=[
            SidecarStatus(ok=True, detail="poll-1", state="running"),
            SidecarStatus(ok=True, detail="poll-2", state="running"),
            SidecarStatus(ok=True, detail="poll-3", state="running"),
        ],
    )
    sup = SidecarSupervisor(poll_interval_seconds=0.05)
    try:
        await sup.start(
            pack_id="vendor-fake",
            release="1.0.0",
            feature_id="tunnel",
            adapter=adapter,  # type: ignore[arg-type]
        )
        # let several polls fire
        await asyncio.sleep(0.25)
        handle = sup._handles[("vendor-fake", "1.0.0", "tunnel")]  # type: ignore[attr-defined]
        # at least one status poll happened
        status_actions = [a for a, _ in adapter.calls if a == "status"]
        assert len(status_actions) >= 1
        # handle's last_status reflects a status poll
        assert handle.last_status.detail.startswith("poll-")
    finally:
        await sup.shutdown()


@pytest.mark.asyncio
async def test_poll_loop_stops_when_status_flips_not_ok() -> None:
    adapter = _ScriptedAdapter(
        starts=[SidecarStatus(ok=True, detail="started", state="running")],
        statuses=[
            SidecarStatus(ok=False, detail="dead", state="failed"),
        ],
    )
    sup = SidecarSupervisor(poll_interval_seconds=0.05)
    try:
        await sup.start(
            pack_id="vendor-fake",
            release="1.0.0",
            feature_id="tunnel",
            adapter=adapter,  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.2)
        handle = sup._handles[("vendor-fake", "1.0.0", "tunnel")]  # type: ignore[attr-defined]
        # poll task ended after seeing not-ok status
        assert handle.poll_task is not None
        assert handle.poll_task.done()
        assert handle.last_status.ok is False
        assert handle.last_status.detail == "dead"
        # only one status call recorded — loop exited after first not-ok
        status_calls = [a for a, _ in adapter.calls if a == "status"]
        assert len(status_calls) == 1
    finally:
        await sup.shutdown()


# ---------------------------------------------------------------------------
# Error handling at start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_records_error_and_reraises_on_adapter_exception() -> None:
    adapter = _RaisingAdapter()
    sup = SidecarSupervisor(poll_interval_seconds=0.05)
    try:
        with pytest.raises(AdapterHookExecutionError):
            await sup.start(
                pack_id="vendor-bad",
                release="1.0.0",
                feature_id="tunnel",
                adapter=adapter,  # type: ignore[arg-type]
            )
        snap = sup.status_snapshot()
        assert len(snap) == 1
        entry = snap[0]
        assert entry["ok"] is False
        assert entry["last_error"] is not None
        assert "sidecar boom" in entry["last_error"]
    finally:
        await sup.shutdown()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_cancels_all_poll_tasks() -> None:
    adapter_a = _ScriptedAdapter()
    adapter_b = _ScriptedAdapter()
    sup = SidecarSupervisor(poll_interval_seconds=0.05)
    await sup.start(
        pack_id="vendor-fake",
        release="1.0.0",
        feature_id="tunnel",
        adapter=adapter_a,  # type: ignore[arg-type]
    )
    await sup.start(
        pack_id="vendor-fake",
        release="1.0.0",
        feature_id="bridge",
        adapter=adapter_b,  # type: ignore[arg-type]
    )
    tasks: list[asyncio.Task[None]] = []
    for handle in sup._handles.values():  # type: ignore[attr-defined]
        assert isinstance(handle, SidecarHandle)
        if handle.poll_task is not None:
            tasks.append(handle.poll_task)
    assert len(tasks) == 2

    await sup.shutdown()

    for task in tasks:
        assert task.done()
    # supervisor empty after shutdown
    assert sup.status_snapshot() == []
