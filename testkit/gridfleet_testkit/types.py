"""Shared public typing helpers for flexible GridFleet payloads."""

from __future__ import annotations

from typing import Literal, TypeAlias, TypedDict

JsonScalar: TypeAlias = "None | bool | int | float | str"
JsonValue: TypeAlias = "None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]"
JsonObject: TypeAlias = "dict[str, JsonValue]"
JsonObjectList: TypeAlias = "list[JsonObject]"
QueryParamValue: TypeAlias = str | int | float | bool | None


class CooldownSetResult(TypedDict):
    status: Literal["cooldown_set"]
    excluded_until: str
    cooldown_count: int


class CooldownEscalatedResult(TypedDict):
    status: Literal["maintenance_escalated", "released"]
    cooldown_count: int
    threshold: int


CooldownResult = CooldownSetResult | CooldownEscalatedResult
