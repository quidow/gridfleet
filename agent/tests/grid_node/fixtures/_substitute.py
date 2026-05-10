from __future__ import annotations

type JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]

PLACEHOLDERS: dict[str, JsonValue] = {
    "<TIMESTAMP>": 1_700_000_000.0,
    "<NODE_ID>": "11111111-1111-4111-8111-111111111111",
    "<SESSION_ID>": "22222222-2222-4222-8222-222222222222",
    "<URI>": "http://127.0.0.1:5555",
}


def substitute_placeholders(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return PLACEHOLDERS.get(value, value)
    if isinstance(value, list):
        return [substitute_placeholders(item) for item in value]
    if isinstance(value, dict):
        return {key: substitute_placeholders(item) for key, item in value.items()}
    return value
