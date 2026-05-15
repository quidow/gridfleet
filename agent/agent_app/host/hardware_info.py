"""Collect static host hardware/OS metadata for registration."""

from __future__ import annotations

import platform
import subprocess
from typing import TYPE_CHECKING, Any

import psutil  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from collections.abc import Callable

_MB = 1024 * 1024
_GB = 1024**3
_SUBPROCESS_TIMEOUT_SEC = 5


def _safe[T](fn: Callable[[], T]) -> T | None:
    try:
        return fn()
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _os_version_darwin() -> str | None:
    product = subprocess.check_output(["sw_vers", "-productName"], text=True, timeout=_SUBPROCESS_TIMEOUT_SEC).strip()
    version = subprocess.check_output(
        ["sw_vers", "-productVersion"], text=True, timeout=_SUBPROCESS_TIMEOUT_SEC
    ).strip()
    return f"{product} {version}".strip() or None


def _os_version_linux() -> str | None:
    with open("/etc/os-release") as fh:
        for line in fh:
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"') or None
    return None


def _os_version() -> str | None:
    system = platform.system()
    if system == "Darwin":
        return _safe(_os_version_darwin)
    if system == "Linux":
        return _safe(_os_version_linux)
    return None


def _cpu_model_darwin() -> str | None:
    out = subprocess.check_output(
        ["sysctl", "-n", "machdep.cpu.brand_string"], text=True, timeout=_SUBPROCESS_TIMEOUT_SEC
    ).strip()
    return out or None


def _cpu_model_linux() -> str | None:
    with open("/proc/cpuinfo") as fh:
        for line in fh:
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip() or None
    return None


def _cpu_model() -> str | None:
    system = platform.system()
    if system == "Darwin":
        return _safe(_cpu_model_darwin)
    if system == "Linux":
        return _safe(_cpu_model_linux)
    return None


def _total_memory_mb() -> int | None:
    return _safe(lambda: int(psutil.virtual_memory().total / _MB))


def _total_disk_gb() -> int | None:
    return _safe(lambda: int(psutil.disk_usage("/").total / _GB))


def _cpu_cores() -> int | None:
    return _safe(lambda: psutil.cpu_count(logical=True))


def _kernel_version() -> str | None:
    uname = _safe(platform.uname)
    if uname is None:
        return None
    return f"{uname.system} {uname.release}".strip() or None


def _cpu_arch() -> str | None:
    uname = _safe(platform.uname)
    if uname is None:
        return None
    return uname.machine or None


_cached: dict[str, Any] | None = None


def collect() -> dict[str, Any]:
    """Return a stable snapshot of host hardware/OS metadata.

    Memoizes only fully-populated snapshots so a transient probe failure on
    first call (e.g. ``psutil.disk_usage`` race during boot) does not freeze
    ``None`` for the process lifetime.
    """
    global _cached
    if _cached is not None:
        return _cached
    snapshot: dict[str, Any] = {
        "os_version": _os_version(),
        "kernel_version": _kernel_version(),
        "cpu_arch": _cpu_arch(),
        "cpu_model": _cpu_model(),
        "cpu_cores": _cpu_cores(),
        "total_memory_mb": _total_memory_mb(),
        "total_disk_gb": _total_disk_gb(),
    }
    if all(v is not None for v in snapshot.values()):
        _cached = snapshot
    return snapshot
