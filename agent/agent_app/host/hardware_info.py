"""Collect static host hardware/OS metadata for registration."""

from __future__ import annotations

import os
import platform
import subprocess
from typing import TYPE_CHECKING, Any

import psutil  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from collections.abc import Callable

_MB = 1024 * 1024
# Disk reported in decimal GB (1 GB = 10^9 bytes) to match Apple Storage and
# `df -H`. Memory stays binary MiB (RAM convention).
_DISK_GB = 1_000_000_000
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


def host_disk_path() -> str:
    """Return the filesystem path that reflects user-data disk usage.

    On macOS APFS the root mount `/` is a sealed system volume holding only OS
    files (~12 GiB). Real user data lives on the data volume mounted at
    `/System/Volumes/Data`, which shares the APFS container so totals match.
    Using `/` makes `disk_used_gb` report system-only usage and miss user data.
    """
    if platform.system() == "Darwin":
        data_volume = "/System/Volumes/Data"
        if os.path.isdir(data_volume):
            return data_volume
    return "/"


def _total_disk_gb() -> int | None:
    return _safe(lambda: int(psutil.disk_usage(host_disk_path()).total / _DISK_GB))


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


# Zero- or one-entry list acts as a mutable singleton cache without
# requiring ``global`` (which CodeQL's unused-global-variable rule
# misreports as dead code).
_cache: list[dict[str, Any]] = []


def collect() -> dict[str, Any]:
    """Return a stable snapshot of host hardware/OS metadata.

    Memoizes only fully-populated snapshots so a transient probe failure on
    first call (e.g. ``psutil.disk_usage`` race during boot) does not freeze
    ``None`` for the process lifetime.
    """
    if _cache:
        return _cache[0]
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
        _cache.append(snapshot)
    return snapshot
