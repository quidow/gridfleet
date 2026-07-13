from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, Integer, String, func
from sqlalchemy.dialects.postgresql import JSON, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class OSType(enum.StrEnum):
    linux = "linux"
    macos = "macos"


class HostStatus(enum.StrEnum):
    online = "online"
    offline = "offline"
    pending = "pending"


class Host(Base):
    __tablename__ = "hosts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hostname: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    ip: Mapped[str] = mapped_column(String, nullable=False)
    os_type: Mapped[OSType] = mapped_column(Enum(OSType), nullable=False)
    agent_port: Mapped[int] = mapped_column(Integer, default=5100, nullable=False)
    status: Mapped[HostStatus] = mapped_column(Enum(HostStatus), default=HostStatus.offline, nullable=False)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_version: Mapped[str | None] = mapped_column(String, nullable=True)
    os_version: Mapped[str | None] = mapped_column(String, nullable=True)
    kernel_version: Mapped[str | None] = mapped_column(String, nullable=True)
    cpu_arch: Mapped[str | None] = mapped_column(String, nullable=True)
    cpu_model: Mapped[str | None] = mapped_column(String, nullable=True)
    cpu_cores: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_disk_gb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capabilities: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tool_env: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    # Registration-bound boot fence: the boot_id the agent last registered under.
    # A status push carrying a different boot_id is a superseded/split-brain boot
    # and is rejected (see app.hosts.service_status_push).
    current_boot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Per-section ingest cursor for the two moved health folds: section name ->
    # {boot_id, section_sequence, payload_sha256, revision}. Decides whether a
    # pushed section is a genuinely new generation (draw+stamp a fresh revision)
    # or a re-delivery (reuse the stamped revision).
    observation_cursors: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # Loop section-skip watermark: section name -> highest revision for which
    # every device has been applied by the StatusFoldLoop. Written by the loop
    # under this host row's lock.
    observation_applied: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    devices: Mapped[list[Any]] = relationship("Device", back_populates="host")

    @property
    def missing_prerequisites(self) -> list[str]:
        capabilities = self.capabilities if isinstance(self.capabilities, dict) else {}
        missing = capabilities.get("missing_prerequisites")
        if not isinstance(missing, list):
            return []
        return [item for item in missing if isinstance(item, str)]
