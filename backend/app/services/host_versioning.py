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


def _normalized_setting(key: str) -> str | None:
    value = settings_service.get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def get_required_agent_version() -> str | None:
    return _normalized_setting("agent.min_version")


def get_recommended_agent_version() -> str | None:
    return _normalized_setting("agent.recommended_version")


def is_agent_update_available(agent_version: str | None) -> bool:
    recommended = get_recommended_agent_version()
    if recommended is None:
        return False
    recommended_parts = _parse_version_parts(recommended)
    agent_parts = _parse_version_parts(agent_version)
    if recommended_parts is None or agent_parts is None:
        return False
    max_len = max(len(recommended_parts), len(agent_parts))
    left = agent_parts + (0,) * (max_len - len(agent_parts))
    right = recommended_parts + (0,) * (max_len - len(recommended_parts))
    return left < right


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
