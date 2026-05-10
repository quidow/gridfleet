from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_app.grid_node.protocol import Slot

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


@dataclass(frozen=True)
class SlotSnapshot:
    slot_id: str
    state: RuntimeSlotState
    reservation_id: str | None
    session_id: str | None
    reserved_at: float | None
    started_at: float | None


@dataclass(frozen=True)
class NodeSnapshot:
    slots: list[SlotSnapshot]
    drain: bool


class NodeState:
    def __init__(self, *, slots: list[Slot], now: Callable[[], float]) -> None:
        self._slots = [_SlotRuntime(slot=slot) for slot in slots]
        self._now = now
        self._drain = False

    def reserve(self, caps: dict[str, Any]) -> Reservation:
        matching = [runtime for runtime in self._slots if self._caps_match(runtime.slot.stereotype.caps, caps)]
        if not matching:
            raise NoMatchingSlotError(f"no slot matches capabilities: {caps!r}")
        if self._drain:
            raise NoFreeSlotError("node is draining")
        for runtime in matching:
            if runtime.state == "FREE":
                reservation_id = str(uuid4())
                runtime.state = "RESERVED"
                runtime.reservation_id = reservation_id
                runtime.reserved_at = self._now()
                return Reservation(id=reservation_id, slot_id=runtime.slot.id)
        raise NoFreeSlotError(f"all matching slots are busy or reserved: {caps!r}")

    def commit(self, reservation_id: str, *, session_id: str, started_at: float) -> None:
        for runtime in self._slots:
            if runtime.reservation_id == reservation_id and runtime.state == "RESERVED":
                runtime.state = "BUSY"
                runtime.reservation_id = None
                runtime.reserved_at = None
                runtime.session_id = session_id
                runtime.started_at = started_at
                return
        raise ReservationGoneError(f"reservation is not active: {reservation_id}")

    def abort(self, reservation_id: str) -> None:
        for runtime in self._slots:
            if runtime.reservation_id == reservation_id and runtime.state == "RESERVED":
                runtime.state = "FREE"
                runtime.reservation_id = None
                runtime.reserved_at = None
                return

    def release(self, session_id: str) -> None:
        for runtime in self._slots:
            if runtime.session_id == session_id and runtime.state == "BUSY":
                runtime.state = "FREE"
                runtime.session_id = None
                runtime.started_at = None
                return

    def expire_reservations(self, *, now: float, ttl_sec: float = 30.0) -> list[str]:
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
        self._drain = True

    def expire_idle(self, *, now: float, timeout_sec: float) -> list[str]:
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
        return NodeSnapshot(
            slots=[
                SlotSnapshot(
                    slot_id=runtime.slot.id,
                    state=runtime.state,
                    reservation_id=runtime.reservation_id,
                    session_id=runtime.session_id,
                    reserved_at=runtime.reserved_at,
                    started_at=runtime.started_at,
                )
                for runtime in self._slots
            ],
            drain=self._drain,
        )

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
