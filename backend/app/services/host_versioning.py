from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING

from app.services.settings_service import settings_service

if TYPE_CHECKING:
    from app.models.host import Host


class AgentVersionStatus(StrEnum):
    disabled = "disabled"
    ok = "ok"
    outdated = "outdated"
    unknown = "unknown"


_VERSION_PARTS_RE = re.compile(r"\d+")


def get_required_agent_version() -> str | None:
    required = settings_service.get("agent.min_version")
    if not isinstance(required, str):
        return None
    normalized = required.strip()
    return normalized or None


def _parse_version_parts(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    parts = tuple(int(match) for match in _VERSION_PARTS_RE.findall(value))
    return parts or None


def get_agent_version_status(host: Host) -> AgentVersionStatus:
    required = get_required_agent_version()
    if required is None:
        return AgentVersionStatus.disabled

    required_parts = _parse_version_parts(required)
    host_parts = _parse_version_parts(host.agent_version)
    if required_parts is None or host_parts is None:
        return AgentVersionStatus.unknown

    max_len = max(len(required_parts), len(host_parts))
    left = host_parts + (0,) * (max_len - len(host_parts))
    right = required_parts + (0,) * (max_len - len(required_parts))
    if left < right:
        return AgentVersionStatus.outdated
    return AgentVersionStatus.ok
