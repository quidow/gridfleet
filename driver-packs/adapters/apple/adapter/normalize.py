"""Apple device identity normalization."""

from __future__ import annotations

from agent_app.pack.adapter_types import FieldError, NormalizedDevice, NormalizeDeviceContext


async def normalize_device(ctx: NormalizeDeviceContext) -> NormalizedDevice:
    raw = ctx.raw_input
    udid = str(raw.get("connection_target") or raw.get("identity_value") or "")
    errors: list[FieldError] = []
    if not udid:
        errors.append(FieldError(field_id="connection_target", message="UDID required for Apple devices"))
    device_type = str(raw.get("device_type") or "real_device")
    requested_connection_type = str(raw.get("connection_type") or "")
    connection_type = "virtual" if device_type != "real_device" else requested_connection_type or "usb"
    if connection_type not in {"usb", "network", "virtual"}:
        connection_type = "usb" if device_type == "real_device" else "virtual"
    return NormalizedDevice(
        identity_scheme="apple_udid",
        identity_scope="host" if device_type == "simulator" else "global",
        identity_value=udid,
        connection_target=udid,
        ip_address="",
        device_type=device_type,
        connection_type=connection_type,
        os_version=str(raw.get("os_version") or ""),
        field_errors=errors,
    )
