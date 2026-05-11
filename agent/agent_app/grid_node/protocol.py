from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

SlotState = Literal["AVAILABLE", "RESERVED", "BUSY"]


class EventType(StrEnum):
    # Wire values match Selenium Grid 4 EventName so the hub's event-bus subscribers
    # accept these messages. Do NOT change without checking Selenium's EventName enum.
    NODE_ADDED = "node-added"
    NODE_STATUS = "node-heartbeat"
    SESSION_STARTED = "session-created"
    SESSION_CLOSED = "session-closed"
    NODE_DRAIN = "node-drain-started"
    NODE_DRAIN_COMPLETE = "node-drain-complete"
    NODE_REMOVED = "node-removed"


@dataclass(frozen=True)
class Stereotype:
    caps: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.caps)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Stereotype:
        return cls(caps=dict(payload))


@dataclass(frozen=True)
class Slot:
    id: str
    stereotype: Stereotype
    state: SlotState = "AVAILABLE"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "state": self.state, "stereotype": self.stereotype.to_dict()}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Slot:
        return cls(
            id=str(payload["id"]),
            state=payload["state"],
            stereotype=Stereotype.from_dict(dict(payload["stereotype"])),
        )


def event_envelope(event_type: EventType, payload: object) -> dict[str, Any]:
    """Internal envelope dict. Wire format is produced by `encode_event_frames`."""
    return {"data": payload, "type": event_type.value}


def build_slots(*, base_caps: dict[str, Any], grid_slots: list[str]) -> list[Slot]:
    slots: list[Slot] = []
    for slot_name in grid_slots:
        caps = dict(base_caps)
        caps.setdefault("gridfleet:run_id", "free")
        if slot_name == "chrome":
            # W3C and Selenium clients send `browserName` lowercase; capitalising
            # this would make capability matching fail for any standards-compliant
            # client request.
            caps["browserName"] = "chrome"
        slots.append(Slot(id=str(uuid4()), stereotype=Stereotype(caps=caps)))
    return slots
