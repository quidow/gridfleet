from __future__ import annotations

from typing import Any


def manifest_supports_host_os(manifest: dict[str, Any], host_os: str) -> bool:
    requires = manifest.get("requires")
    if not isinstance(requires, dict):
        return True
    host_os_values = requires.get("host_os") or []
    if not isinstance(host_os_values, list) or not host_os_values:
        return True
    return host_os in {value for value in host_os_values if isinstance(value, str)}
