"""Stable, agent-owned response schemas keep their root closed.

Adapter-fed responses (PackDevice*, PackDeviceCandidate, HealthCheckResult,
AppiumStatusResponse) are intentionally permissive — adapter authors return
freeform property bags that the agent forwards verbatim. Locking those down
breaks the contract documented in ``CLAUDE.md`` ("dynamic JSON-column or
third-party subfields may stay flexible inside typed envelopes").
"""

from __future__ import annotations

from typing import Any

from agent_app.main import app

STABLE_RESPONSE_SCHEMAS = {
    "HealthResponse",
    "HostTelemetryResponse",
}


def test_stable_response_schemas_forbid_root_extras() -> None:
    spec: dict[str, Any] = app.openapi()
    schemas: dict[str, Any] = spec["components"]["schemas"]
    offenders = [
        name for name in sorted(STABLE_RESPONSE_SCHEMAS) if schemas.get(name, {}).get("additionalProperties") is True
    ]
    assert offenders == []
