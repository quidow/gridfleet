"""Roku ECP discovery via SSDP."""

from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from agent_app.pack.adapter_types import DiscoveryCandidate, DiscoveryContext, FieldError

from adapter.normalize import fetch_device_info

SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_TIMEOUT_SECONDS = 2.0
SSDP_REQUEST = (
    "M-SEARCH * HTTP/1.1\r\n"
    "Host: 239.255.255.250:1900\r\n"
    'Man: "ssdp:discover"\r\n'
    "ST: roku:ecp\r\n"
    "\r\n"
)


@dataclass(frozen=True)
class SsdpDevice:
    ip_address: str
    serial_number: str


def _parse_headers(payload: bytes) -> dict[str, str]:
    text = payload.decode("utf-8", errors="replace")
    headers: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return headers


def _serial_from_usn(value: str) -> str:
    prefix = "uuid:roku:ecp:"
    if value.lower().startswith(prefix):
        return value[len(prefix) :].strip()
    return ""


def parse_ssdp_response(payload: bytes) -> SsdpDevice | None:
    headers = _parse_headers(payload)
    if headers.get("st", "").lower() != "roku:ecp":
        return None
    parsed = urlparse(headers.get("location", ""))
    serial_number = _serial_from_usn(headers.get("usn", ""))
    if not parsed.hostname or not serial_number:
        return None
    return SsdpDevice(ip_address=parsed.hostname, serial_number=serial_number)


def _ssdp_search_sync(timeout: float) -> list[SsdpDevice]:
    deadline = time.monotonic() + timeout
    devices: dict[str, SsdpDevice] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(timeout)
        try:
            sock.sendto(SSDP_REQUEST.encode("ascii"), SSDP_ADDR)
        except OSError:
            return []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                payload, _addr = sock.recvfrom(8192)
            except TimeoutError:
                break
            except OSError:
                break
            parsed = parse_ssdp_response(payload)
            if parsed is not None:
                devices[parsed.serial_number] = parsed
    return list(devices.values())


async def ssdp_search(timeout: float = SSDP_TIMEOUT_SECONDS) -> list[SsdpDevice]:
    return await asyncio.to_thread(_ssdp_search_sync, timeout)


def _software_versions(device_info: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in {
            "roku_os": device_info.get("software-version", ""),
            "build": device_info.get("software-build", ""),
        }.items()
        if value
    }


def _detected_properties(
    device: SsdpDevice,
    device_info: dict[str, str],
) -> dict[str, object]:
    return {
        "connection_target": device.ip_address,
        "ip_address": device.ip_address,
        "device_type": "real_device",
        "connection_type": "network",
        "manufacturer": device_info.get("vendor-name", "") or "Roku",
        "model": device_info.get("model-name") or device_info.get("model-number", ""),
        "model_number": device_info.get("model-number", ""),
        "os_version": device_info.get("software-version", ""),
        "software_versions": _software_versions(device_info),
    }


async def discover_roku_devices(ctx: DiscoveryContext) -> list[DiscoveryCandidate]:
    devices = await ssdp_search()
    if not devices:
        return []
    infos = await asyncio.gather(
        *(fetch_device_info(device.ip_address) for device in devices),
        return_exceptions=True,
    )
    candidates: list[DiscoveryCandidate] = []
    for device, info in zip(devices, infos, strict=True):
        if isinstance(info, BaseException):
            candidates.append(
                DiscoveryCandidate(
                    identity_scheme="roku_serial",
                    identity_value=device.serial_number,
                    suggested_name=device.serial_number,
                    detected_properties={
                        "connection_target": device.ip_address,
                        "ip_address": device.ip_address,
                        "device_type": "real_device",
                        "connection_type": "network",
                    },
                    runnable=False,
                    missing_requirements=["ecp_device_info"],
                    field_errors=[
                        FieldError(
                            field_id="ip_address",
                            message=f"Unable to query Roku ECP device-info: {info}",
                        )
                    ],
                    feature_status=[],
                )
            )
            continue
        serial_number = info.get("serial-number") or info.get("device-id") or device.serial_number
        model = info.get("model-name") or info.get("model-number") or serial_number
        candidates.append(
            DiscoveryCandidate(
                identity_scheme="roku_serial",
                identity_value=serial_number,
                suggested_name=model,
                detected_properties=_detected_properties(device, info),
                runnable=True,
                missing_requirements=[],
                field_errors=[],
                feature_status=[],
            )
        )
    return candidates
