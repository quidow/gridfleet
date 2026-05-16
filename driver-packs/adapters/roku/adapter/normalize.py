"""Roku device identity normalization."""

from __future__ import annotations

import asyncio
import urllib.request
import xml.etree.ElementTree as ET

from agent_app.pack.adapter_types import FieldError, NormalizedDevice, NormalizeDeviceContext


async def fetch_device_info(ip_address: str) -> dict[str, str]:
    def _fetch() -> dict[str, str]:
        with urllib.request.urlopen(f"http://{ip_address}:8060/query/device-info", timeout=3) as response:
            payload = response.read()
        root = ET.fromstring(payload)
        return {child.tag: child.text or "" for child in root}

    return await asyncio.to_thread(_fetch)


async def normalize_device(ctx: NormalizeDeviceContext) -> NormalizedDevice:
    raw = ctx.raw_input
    ip_address = str(raw.get("ip_address") or raw.get("connection_target") or "")
    serial = str(raw.get("serial_number") or "")
    os_version = str(raw.get("os_version") or "")
    if os_version.lower() == "unknown":
        os_version = ""
    manufacturer = str(raw.get("manufacturer") or "")
    model = str(raw.get("model") or "")
    model_number = str(raw.get("model_number") or "")
    software_versions: dict[str, str] = {}
    errors: list[FieldError] = []

    if not serial and ip_address:
        try:
            device_info = await fetch_device_info(ip_address)
        except Exception as exc:
            errors.append(FieldError(field_id="ip_address", message=f"Unable to query Roku ECP device-info: {exc}"))
            device_info = {}
        serial = device_info.get("serial-number") or device_info.get("device-id") or ""
        os_version = os_version or device_info.get("software-version", "")
        manufacturer = manufacturer or device_info.get("vendor-name", "") or "Roku"
        model = model or device_info.get("model-name") or device_info.get("model-number", "")
        model_number = model_number or device_info.get("model-number", "")
        software_versions = {
            key: value
            for key, value in {
                "roku_os": device_info.get("software-version", ""),
                "build": device_info.get("software-build", ""),
            }.items()
            if value
        }

    if not serial:
        errors.append(FieldError(field_id="serial_number", message="Roku serial number required"))
    return NormalizedDevice(
        identity_scheme="roku_serial",
        identity_scope="global",
        identity_value=serial,
        connection_target=ip_address,
        ip_address=ip_address,
        device_type="real_device",
        connection_type="network",
        os_version=os_version,
        manufacturer=manufacturer,
        model=model,
        model_number=model_number,
        software_versions=software_versions,
        field_errors=errors,
    )
