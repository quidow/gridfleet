"""Shared public typing helpers for flexible GridFleet payloads."""

from __future__ import annotations

from typing import TypeAlias

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = object
JsonObject: TypeAlias = dict[str, JsonValue]
JsonObjectList: TypeAlias = list[JsonObject]
QueryParamValue: TypeAlias = str | int | float | bool | None
