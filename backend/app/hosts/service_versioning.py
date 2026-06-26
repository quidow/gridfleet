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
    return _is_below(agent_version, recommended_version) is True


def _parse_version_parts(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    parts = tuple(int(match) for match in _VERSION_PARTS_RE.findall(value))
    return parts or None


def _is_below(left_version: str | None, right_version: str | None) -> bool | None:
    left_parts = _parse_version_parts(left_version)
    right_parts = _parse_version_parts(right_version)
    if left_parts is None or right_parts is None:
        return None
    max_len = max(len(left_parts), len(right_parts))
    left = left_parts + (0,) * (max_len - len(left_parts))
    right = right_parts + (0,) * (max_len - len(right_parts))
    return left < right


def get_agent_version_status(agent_version: str | None, required_version: str | None) -> AgentVersionStatus:
    if required_version is None:
        return AgentVersionStatus.disabled
    below = _is_below(agent_version, required_version)
    if below is None:
        return AgentVersionStatus.unknown
    return AgentVersionStatus.outdated if below else AgentVersionStatus.ok
