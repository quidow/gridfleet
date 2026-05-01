import asyncio

import pytest

from agent_app.pack import sidecar_supervisor as sup_mod
from agent_app.pack.adapter_types import SidecarStatus
from agent_app.pack.sidecar_supervisor import SidecarSupervisor

pytestmark = pytest.mark.asyncio


class _StubAdapter:
    pack_id = "pack-x"
    pack_release = "0.0.1"

    async def sidecar_lifecycle(self, *, feature_id: str, action: str) -> dict[str, object]:
        return {"ok": True, "detail": action, "state": "running"}


async def test_poll_loop_does_not_mutate_handle_after_stop_removed_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation-resistant status call that returns after `stop()` has
    detached the handle must not write to that detached handle."""

    sup = SidecarSupervisor(poll_interval_seconds=0.01)
    adapter = _StubAdapter()
    status_started = asyncio.Event()
    cancellation_observed = asyncio.Event()

    async def fake_dispatch(adapter: object, feature_id: str, action: str) -> SidecarStatus:
        if action == "status":
            status_started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                # Adapter hooks are third-party code. A hook can catch
                # cancellation during cleanup and still return a status.
                cancellation_observed.set()
            return SidecarStatus(ok=False, detail="poll-after-stop", state="error")
        if action == "stop":
            return SidecarStatus(ok=True, detail="stopped", state="stopped")
        return SidecarStatus(ok=True, detail="started", state="running")

    monkeypatch.setattr(sup_mod, "dispatch_sidecar_lifecycle", fake_dispatch)

    await sup.start(pack_id="pack-x", release="0.0.1", feature_id="feat", adapter=adapter)

    # Capture the handle as start() stored it. After stop() returns, this
    # detached object must NOT have been mutated by an in-flight poll.
    handle = next(iter(sup._handles.values()))
    handle.last_status = SidecarStatus(ok=True, detail="initial", state="running")
    handle.last_error = None

    # Wait until `_poll_loop` has captured the handle and entered the status
    # dispatch. Then `stop()` pops the handle and cancels that poll task.
    await asyncio.wait_for(status_started.wait(), timeout=1)

    await sup.stop(pack_id="pack-x", release="0.0.1", feature_id="feat", adapter=adapter)
    assert cancellation_observed.is_set(), "test did not exercise the cancellation-resistant status path"

    # Post-fix invariants:
    assert sup._handles == {}, "stopped sidecar still tracked"
    assert handle.last_status.detail == "initial", (
        f"poll loop wrote to a handle after stop() removed it — observed last_status={handle.last_status}"
    )
    assert handle.last_error is None, f"poll loop wrote last_error after detach: {handle.last_error!r}"
