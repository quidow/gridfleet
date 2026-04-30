"""Apple real-device telemetry via go-ios."""

from __future__ import annotations

import json

from agent_app.pack.adapter_types import HardwareTelemetry, TelemetryContext
from agent_app.pack.adapter_utils import run_cmd

from adapter.tools import find_go_ios


async def collect_telemetry(ctx: TelemetryContext) -> HardwareTelemetry:
    ios = find_go_ios()
    if not ios:
        return HardwareTelemetry(supported=False)

    registry = await run_cmd([ios, "batteryregistry", f"--udid={ctx.connection_target}"], timeout=10)
    telemetry = _telemetry_from_registry(_last_json_object(registry))
    if telemetry.supported:
        return telemetry

    battery = await run_cmd([ios, "batterycheck", f"--udid={ctx.connection_target}"], timeout=10)
    return _telemetry_from_batterycheck(_last_json_object(battery))


def _last_json_object(output: str) -> dict[str, object]:
    decoder = json.JSONDecoder()
    parsed: list[dict[str, object]] = []
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            parsed.append(value)
    return parsed[-1] if parsed else {}


def _telemetry_from_registry(data: dict[str, object]) -> HardwareTelemetry:
    if not data:
        return HardwareTelemetry(supported=False)
    level = _coerce_int(data.get("CurrentCapacity"))
    temperature = _coerce_temperature_c(data.get("Temperature"))
    charging_state = _charging_state(data.get("IsCharging"))
    if level is None and temperature is None and charging_state is None:
        return HardwareTelemetry(supported=False)
    return HardwareTelemetry(
        supported=True,
        battery_level_percent=level,
        battery_temperature_c=temperature,
        charging_state=charging_state,
    )


def _telemetry_from_batterycheck(data: dict[str, object]) -> HardwareTelemetry:
    if not data or data.get("HasBattery") is False:
        return HardwareTelemetry(supported=False)
    level = _coerce_int(data.get("BatteryCurrentCapacity"))
    charging_state = _charging_state(data.get("BatteryIsCharging"))
    if level is None and charging_state is None:
        return HardwareTelemetry(supported=False)
    return HardwareTelemetry(
        supported=True,
        battery_level_percent=level,
        charging_state=charging_state,
    )


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_temperature_c(value: object) -> float | None:
    raw = _coerce_int(value)
    if raw is None:
        return None
    return raw / 100.0


def _charging_state(value: object) -> str | None:
    if value is True:
        return "charging"
    if value is False:
        return "discharging"
    return None
