from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class AgentVersionGuidance:
    required_agent_version: str | None = None
    recommended_agent_version: str | None = None
    agent_version_status: str | None = None
    agent_update_available: bool = False

    def to_payload(self) -> dict[str, str | bool | None]:
        return asdict(self)


_guidance = AgentVersionGuidance()


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def get_version_guidance() -> AgentVersionGuidance:
    return _guidance


def clear_version_guidance() -> None:
    global _guidance
    _guidance = AgentVersionGuidance()


def update_version_guidance(payload: dict[str, Any]) -> bool:
    global _guidance
    raw_update = payload.get("agent_update_available")
    next_guidance = AgentVersionGuidance(
        required_agent_version=_optional_str(payload.get("required_agent_version")),
        recommended_agent_version=_optional_str(payload.get("recommended_agent_version")),
        agent_version_status=_optional_str(payload.get("agent_version_status")),
        agent_update_available=raw_update is True,
    )
    changed = next_guidance != _guidance
    _guidance = next_guidance
    return changed
