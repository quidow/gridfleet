from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class ProbeTargetOut(BaseModel):
    device_id: uuid.UUID
    connection_target: str
    identity_value: str | None = None
    pack_id: str
    platform_id: str
    device_type: str
    connection_type: str | None = None
    ip_address: str | None = None
    headless: bool | None = None
    allow_boot: bool = False
    ip_ping_timeout_sec: float | None = None
    ip_ping_count: int | None = None
    claimed_ports: dict[str, int] = Field(default_factory=dict)
    lifecycle_state_capable: bool = False


class ProbeTargetsOut(BaseModel):
    host_id: uuid.UUID
    devices: list[ProbeTargetOut]
