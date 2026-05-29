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


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class VersionGuidanceStore:
    """Holds the latest agent-version guidance returned by the manager."""

    def __init__(self) -> None:
        self._guidance = AgentVersionGuidance()

    def get(self) -> AgentVersionGuidance:
        return self._guidance

    def update(self, payload: dict[str, Any]) -> bool:
        raw_update = payload.get("agent_update_available")
        next_guidance = AgentVersionGuidance(
            required_agent_version=_optional_str(payload.get("required_agent_version")),
            recommended_agent_version=_optional_str(payload.get("recommended_agent_version")),
            agent_version_status=_optional_str(payload.get("agent_version_status")),
            agent_update_available=raw_update is True,
        )
        changed = next_guidance != self._guidance
        self._guidance = next_guidance
        return changed
