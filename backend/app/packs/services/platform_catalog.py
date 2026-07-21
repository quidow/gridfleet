from __future__ import annotations

from typing import Any


def platform_has_lifecycle_action(lifecycle_actions: list[dict[str, Any]], action_id: str) -> bool:
    return any(a.get("id") == action_id for a in lifecycle_actions)
