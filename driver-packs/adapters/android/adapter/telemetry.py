"""Android hardware telemetry collection."""

from __future__ import annotations

from agent_app.pack.adapter_types import HardwareTelemetry, TelemetryContext
from agent_app.pack.adapter_utils import run_cmd

from adapter.tools import find_adb

ANDROID_STATUS_MAP: dict[str, str] = {
    "1": "unknown",
    "2": "charging",
    "3": "discharging",
    "4": "not_charging",
    "5": "full",
}


def _parse_temperature(value: str, *, battery_present: bool) -> float | None:
    try:
        raw = int(value)
    except ValueError:
        return None
    if battery_present or raw > 100:
        return raw / 10.0
    return float(raw)


def _parse_android_dumpsys_battery(output: str) -> HardwareTelemetry:
    parsed: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        parsed[key.strip().lower()] = value.strip()

    battery_present = parsed.get("present", "").lower() != "false"
    if not battery_present:
        temperature = _parse_temperature(parsed.get("temperature", ""), battery_present=False)
        if temperature is None:
            return HardwareTelemetry(supported=False)
        return HardwareTelemetry(supported=True, battery_temperature_c=temperature)

    level: int | None = None
    if parsed.get("level"):
        try:
            level = int(parsed["level"])
        except ValueError:
            level = None
    temperature: float | None = None
    if parsed.get("temperature"):
        temperature = _parse_temperature(parsed["temperature"], battery_present=True)
    status = parsed.get("status")
    return HardwareTelemetry(
        supported=True,
        battery_level_percent=level,
        battery_temperature_c=temperature,
        charging_state=ANDROID_STATUS_MAP.get(status or "", "unknown") if status else None,
    )


async def collect_telemetry(ctx: TelemetryContext) -> HardwareTelemetry:
    adb = find_adb()
    output = await run_cmd([adb, "-s", ctx.connection_target, "shell", "dumpsys", "battery"])
    if not output:
        return HardwareTelemetry(supported=False)
    return _parse_android_dumpsys_battery(output)
