"""Async supervisor for adapter-backed driver-pack sidecars.

A ``SidecarSupervisor`` keeps a per-process registry of started sidecars keyed
by ``(pack_id, release, feature_id)``. For each registered sidecar it owns:

* A ``SidecarHandle`` carrying the last-known ``SidecarStatus`` and any error.
* An optional poll task that calls ``dispatch_sidecar_lifecycle(adapter, ..., "status")``
  every ``poll_interval_seconds`` and updates the handle.

If a poll observes ``ok=False``, the supervisor stops polling — the agent's
status payload will surface the bad state and the backend reconciler decides
whether to restart. The supervisor never restarts on its own.

All adapter calls go through ``adapter_dispatch.dispatch_sidecar_lifecycle``
so that timeout / contract / execution errors are normalised consistently
with the rest of the agent's adapter wiring.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from agent_app.pack.adapter_dispatch import dispatch_sidecar_lifecycle
from agent_app.pack.adapter_types import DriverPackAdapter, SidecarStatus

logger = logging.getLogger(__name__)


_DEFAULT_POLL_INTERVAL_SECONDS: float = 30.0


@dataclass
class SidecarHandle:
    """Per-sidecar runtime state owned by the supervisor."""

    pack_id: str
    release: str
    feature_id: str
    last_status: SidecarStatus
    last_error: str | None = None
    poll_task: asyncio.Task[None] | None = field(default=None, repr=False)


class SidecarSupervisor:
    """Owns the lifetime of adapter-backed sidecars on a single agent process.

    A single instance per agent process. Created in the agent's lifespan and
    torn down via ``shutdown()`` on shutdown.
    """

    def __init__(self, *, poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS) -> None:
        self._poll_interval_seconds = poll_interval_seconds
        self._handles: dict[tuple[str, str, str], SidecarHandle] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def tracked_keys(self) -> set[tuple[str, str, str]]:
        """Return the sidecar keys currently owned by this supervisor."""
        return set(self._handles.keys())

    async def start(
        self,
        *,
        pack_id: str,
        release: str,
        feature_id: str,
        adapter: DriverPackAdapter,
    ) -> SidecarStatus:
        """Start a sidecar via its adapter.

        Idempotent: if a handle already exists with ``last_status.ok=True``,
        the call returns the cached status without re-invoking the adapter.

        On a successful (``ok=True``) start, a poll task is scheduled.
        On ``ok=False`` the handle is stored without a poll task.
        On an adapter exception the error is recorded on the handle and re-raised.
        """
        key = (pack_id, release, feature_id)
        async with self._lock:
            existing = self._handles.get(key)
            if existing is not None and existing.last_status.ok and existing.last_error is None:
                return existing.last_status

            try:
                status = await dispatch_sidecar_lifecycle(adapter, feature_id, "start")
            except Exception as exc:
                handle = SidecarHandle(
                    pack_id=pack_id,
                    release=release,
                    feature_id=feature_id,
                    last_status=SidecarStatus(ok=False, detail=str(exc), state="error"),
                    last_error=str(exc),
                    poll_task=None,
                )
                self._handles[key] = handle
                raise

            handle = SidecarHandle(
                pack_id=pack_id,
                release=release,
                feature_id=feature_id,
                last_status=status,
                last_error=None,
                poll_task=None,
            )
            if status.ok:
                handle.poll_task = asyncio.create_task(
                    self._poll_loop(key, adapter),
                    name=f"sidecar-poll:{pack_id}:{release}:{feature_id}",
                )
            self._handles[key] = handle
            return status

    async def stop(
        self,
        *,
        pack_id: str,
        release: str,
        feature_id: str,
        adapter: DriverPackAdapter,
    ) -> SidecarStatus:
        """Stop a sidecar: cancel its poll task and call the adapter's stop hook.

        The handle is removed from the registry on success. On adapter error the
        handle is also removed but the error is re-raised after cancelling the
        poll task — callers (the backend reconciler) decide what to do next.
        """
        key = (pack_id, release, feature_id)
        async with self._lock:
            handle = self._handles.pop(key, None)

        if handle is not None and handle.poll_task is not None:
            await self._cancel_task(handle.poll_task)

        return await dispatch_sidecar_lifecycle(adapter, feature_id, "stop")

    async def drop(self, *, pack_id: str, release: str, feature_id: str) -> None:
        """Remove a sidecar handle without calling the adapter stop hook.

        Used when desired state removed the pack or the adapter is no longer
        loaded. The poll task already captured the adapter reference, so it
        must be cancelled even when a clean stop hook cannot be dispatched.
        """
        key = (pack_id, release, feature_id)
        async with self._lock:
            handle = self._handles.pop(key, None)
        if handle is not None and handle.poll_task is not None:
            await self._cancel_task(handle.poll_task)

    def status_snapshot(self) -> list[dict[str, Any]]:
        """Return a JSON-serialisable list of every tracked sidecar's state.

        Used by ``/agent/driver-packs/status`` to surface sidecar state to the
        backend reconciler.
        """
        snapshot: list[dict[str, Any]] = []
        for handle in self._handles.values():
            snapshot.append(
                {
                    "pack_id": handle.pack_id,
                    "release": handle.release,
                    "feature_id": handle.feature_id,
                    "ok": handle.last_status.ok,
                    "detail": handle.last_status.detail,
                    "state": handle.last_status.state,
                    "last_error": handle.last_error,
                }
            )
        return snapshot

    async def shutdown(self) -> None:
        """Cancel every poll task and clear the registry.

        Called from the agent's lifespan shutdown. Does not call adapter ``stop``
        — that's a deliberate choice; on agent shutdown we can't guarantee the
        adapter import path is still importable, and the supervisor's contract
        is "best-effort cancellation, no side effects".
        """
        async with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
        for handle in handles:
            if handle.poll_task is not None:
                await self._cancel_task(handle.poll_task)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _poll_loop(
        self,
        key: tuple[str, str, str],
        adapter: DriverPackAdapter,
    ) -> None:
        """Poll the adapter's ``status`` hook until status flips to not-ok or task cancels."""
        pack_id, release, feature_id = key
        try:
            while True:
                try:
                    await asyncio.sleep(self._poll_interval_seconds)
                except asyncio.CancelledError:
                    raise

                # The handle may have been removed by stop() between sleeps.
                handle = self._handles.get(key)
                if handle is None:
                    return

                try:
                    status = await dispatch_sidecar_lifecycle(adapter, feature_id, "status")
                except Exception as exc:
                    handle.last_error = str(exc)
                    handle.last_status = SidecarStatus(ok=False, detail=str(exc), state="error")
                    logger.warning(
                        "sidecar status poll failed for %s/%s/%s: %s",
                        pack_id,
                        release,
                        feature_id,
                        exc,
                    )
                    return

                handle.last_status = status
                handle.last_error = None
                if not status.ok:
                    logger.info(
                        "sidecar %s/%s/%s reported not-ok status; stopping poll loop",
                        pack_id,
                        release,
                        feature_id,
                    )
                    return
        except asyncio.CancelledError:
            # Cancellation is the normal shutdown path; swallow cleanly.
            return

    @staticmethod
    async def _cancel_task(task: asyncio.Task[None]) -> None:
        if task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            return
