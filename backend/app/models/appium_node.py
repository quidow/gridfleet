from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.appium_node_resource_claim import AppiumNodeResourceClaim
    from app.models.device import Device


class NodeState(enum.StrEnum):
    running = "running"
    stopped = "stopped"
    error = "error"


class AppiumNode(Base):
    __tablename__ = "appium_nodes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    grid_url: Mapped[str] = mapped_column(String, nullable=False)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    container_id: Mapped[str | None] = mapped_column(String, nullable=True)
    active_connection_target: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[NodeState] = mapped_column(Enum(NodeState), default=NodeState.stopped, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    live_capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
        default=dict,
    )
    consecutive_health_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_health_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    health_running: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    health_state: Mapped[str | None] = mapped_column(Text, nullable=True)

    device: Mapped[Device] = relationship("Device", back_populates="appium_node")
    resource_claims: Mapped[list[AppiumNodeResourceClaim]] = relationship(
        "AppiumNodeResourceClaim",
        back_populates="node",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
