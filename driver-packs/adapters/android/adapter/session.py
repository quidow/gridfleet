"""Android adapter session hooks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_app.pack.adapter_types import SessionOutcome, SessionSpec


async def pre_session(spec: SessionSpec) -> dict[str, Any]:
    return {"appium:udid": spec.device_identity_value}


async def post_session(spec: SessionSpec, outcome: SessionOutcome) -> None:
    return None
