from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import psutil  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)
_MB = 1024 * 1024
_GB = 1024**3


async def _safe_call[T](metric_name: str, fn: Callable[..., T], *args: object) -> T | None:
    try:
        return await asyncio.to_thread(fn, *args)
    except Exception:
        logger.warning("Failed to read host telemetry metric %s", metric_name, exc_info=True)
        return None


async def get_host_telemetry() -> dict[str, Any]:
    cpu = await _safe_call("cpu_percent", psutil.cpu_percent, 0.2)
    vm = await _safe_call("virtual_memory", psutil.virtual_memory)
    disk = await _safe_call("disk_usage", psutil.disk_usage, "/")

    raw: dict[str, Any] = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "cpu_percent": float(cpu) if cpu is not None else None,
        "memory_used_mb": int(vm.used / _MB) if vm is not None else None,
        "memory_total_mb": int(vm.total / _MB) if vm is not None else None,
        "disk_used_gb": round(disk.used / _GB, 2) if disk is not None else None,
        "disk_total_gb": round(disk.total / _GB, 2) if disk is not None else None,
        "disk_percent": float(disk.percent) if disk is not None else None,
    }
    typed_keys = {
        "recorded_at",
        "cpu_percent",
        "memory_used_mb",
        "memory_total_mb",
        "disk_used_gb",
        "disk_total_gb",
        "disk_percent",
    }
    payload = {key: raw.get(key) for key in typed_keys}
    payload["extras"] = {key: value for key, value in raw.items() if key not in typed_keys}
    return payload
