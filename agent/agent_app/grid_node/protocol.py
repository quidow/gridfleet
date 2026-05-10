from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

SlotState = Literal["AVAILABLE", "RESERVED", "BUSY"]


class EventType(StrEnum):
    NODE_ADDED = "NODE_ADDED"
    NODE_STATUS = "NODE_STATUS"
    SESSION_STARTED = "SESSION_STARTED"
    SESSION_CLOSED = "SESSION_CLOSED"
    NODE_DRAIN = "NODE_DRAIN"
    NODE_DRAIN_COMPLETE = "NODE_DRAIN_COMPLETE"
    NODE_REMOVED = "NODE_REMOVED"


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


def event_envelope(event_type: EventType, payload: dict[str, Any]) -> dict[str, Any]:
    return {"data": payload, "type": event_type.value}


def build_slots(*, base_caps: dict[str, Any], grid_slots: list[str]) -> list[Slot]:
    slots: list[Slot] = []
    for slot_name in grid_slots:
        caps = dict(base_caps)
        if slot_name == "chrome":
            caps["browserName"] = "Chrome"
        slots.append(Slot(id=str(uuid4()), stereotype=Stereotype(caps=caps)))
    return slots
