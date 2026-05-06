from __future__ import annotations

import re
from enum import StrEnum


class AgentVersionStatus(StrEnum):
    disabled = "disabled"
    ok = "ok"
    outdated = "outdated"
    unknown = "unknown"


_VERSION_PARTS_RE = re.compile(r"\d+")


def normalize_agent_version_setting(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def is_agent_update_available(agent_version: str | None, recommended_version: str | None) -> bool:
    if recommended_version is None:
        return False
    recommended_parts = _parse_version_parts(recommended_version)
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


def get_agent_version_status(agent_version: str | None, required_version: str | None) -> AgentVersionStatus:
    if required_version is None:
        return AgentVersionStatus.disabled

    required_parts = _parse_version_parts(required_version)
    host_parts = _parse_version_parts(agent_version)
    if required_parts is None or host_parts is None:
        return AgentVersionStatus.unknown

    max_len = max(len(required_parts), len(host_parts))
    left = host_parts + (0,) * (max_len - len(host_parts))
    right = required_parts + (0,) * (max_len - len(required_parts))
    if left < right:
        return AgentVersionStatus.outdated
    return AgentVersionStatus.ok
