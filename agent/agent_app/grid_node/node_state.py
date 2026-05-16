from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from agent_app.grid_node.protocol import Slot, Stereotype

if TYPE_CHECKING:
    from collections.abc import Callable

RuntimeSlotState = Literal["FREE", "RESERVED", "BUSY"]


class NoMatchingSlotError(Exception):
    """No slot stereotype matches the requested capabilities."""


class NoFreeSlotError(Exception):
    """At least one slot matches the requested capabilities, but none is free."""


class ReservationGoneError(Exception):
    """A reservation was expired, aborted, or never existed."""


@dataclass(frozen=True)
class Reservation:
    id: str
    slot_id: str


@dataclass
class _SlotRuntime:
    slot: Slot
    state: RuntimeSlotState = "FREE"
    reservation_id: str | None = None
    session_id: str | None = None
    reserved_at: float | None = None
    started_at: float | None = None
    # Capabilities returned by Appium and the wall-clock start time are tracked
    # so the hub-facing NodeStatus payload can include real session info on
    # busy slots (the hub UI desyncs without it).
    session_capabilities: dict[str, Any] | None = None
    session_start_iso: str | None = None


@dataclass(frozen=True)
class SlotSnapshot:
    slot_id: str
    state: RuntimeSlotState
    reservation_id: str | None
    session_id: str | None
    reserved_at: float | None
    started_at: float | None
    session_capabilities: dict[str, Any] | None
    session_start_iso: str | None


@dataclass(frozen=True)
class NodeSnapshot:
    slots: list[SlotSnapshot]
    drain: bool


class NodeState:
    # Every method that reads or mutates `_slots` / `_drain` runs under `_lock`.
    # The grid node receives concurrent calls from the http_server (reserve /
    # commit / abort / release) and from the GridNodeService heartbeat /
    # reaper loops (expire_reservations / expire_idle / snapshot). Without
    # serialization, two reserve() callers could both pass the
    # "any slot non-FREE" guard before either flips a slot to RESERVED,
    # double-booking the device that the hub-side maxSessions=1 cap is meant
    # to protect. The single lock also keeps stereotype reads consistent with
    # concurrent update_all_slot_caps writes.
    def __init__(self, *, slots: list[Slot], now: Callable[[], float]) -> None:
        self._slots = [_SlotRuntime(slot=slot) for slot in slots]
        self._now = now
        self._drain = False
        self._lock = threading.Lock()

    def reserve(self, caps: dict[str, Any]) -> Reservation:
        with self._lock:
            matching = [runtime for runtime in self._slots if self._caps_match(runtime.slot.stereotype.caps, caps)]
            if not matching:
                raise NoMatchingSlotError(f"no slot matches capabilities: {caps!r}")
            if self._drain:
                raise NoFreeSlotError("node is draining")
            # A grid node represents one physical device. When the node advertises
            # multiple slots (e.g. Android native + chrome) they are alternate
            # capability profiles for the same device — only one session can run
            # at a time. Reject if any slot is already held, regardless of which
            # stereotype the new request matches.
            if any(runtime.state != "FREE" for runtime in self._slots):
                raise NoFreeSlotError(f"node has an active reservation or session: {caps!r}")
            for runtime in matching:
                if runtime.state == "FREE":
                    reservation_id = str(uuid4())
                    runtime.state = "RESERVED"
                    runtime.reservation_id = reservation_id
                    runtime.reserved_at = self._now()
                    return Reservation(id=reservation_id, slot_id=runtime.slot.id)
            raise NoFreeSlotError(f"all matching slots are busy or reserved: {caps!r}")

    def commit(
        self,
        reservation_id: str,
        *,
        session_id: str,
        started_at: float,
        capabilities: dict[str, Any] | None = None,
        session_start_iso: str | None = None,
    ) -> None:
        with self._lock:
            for runtime in self._slots:
                if runtime.reservation_id == reservation_id and runtime.state == "RESERVED":
                    runtime.state = "BUSY"
                    runtime.reservation_id = None
                    runtime.reserved_at = None
                    runtime.session_id = session_id
                    runtime.started_at = started_at
                    runtime.session_capabilities = dict(capabilities) if capabilities else None
                    runtime.session_start_iso = session_start_iso
                    return
            raise ReservationGoneError(f"reservation is not active: {reservation_id}")

    def abort(self, reservation_id: str) -> None:
        with self._lock:
            for runtime in self._slots:
                if runtime.reservation_id == reservation_id and runtime.state == "RESERVED":
                    runtime.state = "FREE"
                    runtime.reservation_id = None
                    runtime.reserved_at = None
                    return

    def release(self, session_id: str) -> None:
        with self._lock:
            for runtime in self._slots:
                if runtime.session_id == session_id and runtime.state == "BUSY":
                    runtime.state = "FREE"
                    runtime.session_id = None
                    runtime.started_at = None
                    runtime.session_capabilities = None
                    runtime.session_start_iso = None
                    return

    def expire_reservations(self, *, now: float, ttl_sec: float = 30.0) -> list[str]:
        with self._lock:
            expired: list[str] = []
            for runtime in self._slots:
                if runtime.state != "RESERVED" or runtime.reservation_id is None or runtime.reserved_at is None:
                    continue
                if now - runtime.reserved_at >= ttl_sec:
                    expired.append(runtime.reservation_id)
                    runtime.state = "FREE"
                    runtime.reservation_id = None
                    runtime.reserved_at = None
            return expired

    def mark_drain(self) -> None:
        with self._lock:
            self._drain = True

    def expire_idle(self, *, now: float, timeout_sec: float) -> list[str]:
        with self._lock:
            expired: list[str] = []
            for runtime in self._slots:
                if (
                    runtime.state == "BUSY"
                    and runtime.session_id is not None
                    and runtime.started_at is not None
                    and now - runtime.started_at >= timeout_sec
                ):
                    expired.append(runtime.session_id)
            return expired

    def snapshot(self) -> NodeSnapshot:
        with self._lock:
            return NodeSnapshot(
                slots=[
                    SlotSnapshot(
                        slot_id=runtime.slot.id,
                        state=runtime.state,
                        reservation_id=runtime.reservation_id,
                        session_id=runtime.session_id,
                        reserved_at=runtime.reserved_at,
                        started_at=runtime.started_at,
                        session_capabilities=runtime.session_capabilities,
                        session_start_iso=runtime.session_start_iso,
                    )
                    for runtime in self._slots
                ],
                drain=self._drain,
            )

    def update_all_slot_caps(self, updates: dict[str, object]) -> None:
        """Merge ``updates`` into every slot's stereotype, preserving per-slot caps.

        Used for shared fields that change across the whole node (e.g.
        ``gridfleet:run_id``). Per-slot identity caps such as
        ``browserName="chrome"`` on the chrome slot of an Android device MUST
        survive — overwriting the full caps dict from a single source would
        collapse distinct slots into identical stereotypes and the hub would
        stop matching browser sessions.
        """
        with self._lock:
            for runtime in self._slots:
                merged: dict[str, Any] = dict(runtime.slot.stereotype.caps)
                merged.update(updates)
                runtime.slot = Slot(
                    id=runtime.slot.id,
                    state=runtime.slot.state,
                    stereotype=Stereotype(caps=merged),
                )

    def snapshot_slots(self) -> list[Slot]:
        with self._lock:
            return [
                Slot(
                    id=runtime.slot.id,
                    state=runtime.slot.state,
                    stereotype=Stereotype(caps=dict(runtime.slot.stereotype.caps)),
                )
                for runtime in self._slots
            ]

    @classmethod
    def _caps_match(cls, stereotype: dict[str, Any], required: dict[str, Any]) -> bool:
        for key, value in required.items():
            if key not in stereotype:
                return False
            if isinstance(value, dict):
                actual = stereotype[key]
                if not isinstance(actual, dict) or not cls._caps_match(actual, value):
                    return False
            elif stereotype[key] != value:
                return False
        return True
