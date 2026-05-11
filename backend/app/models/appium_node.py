from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class NodeState(enum.StrEnum):
    running = "running"
    stopped = "stopped"
    error = "error"


class AppiumNode(Base):
    __tablename__ = "appium_nodes"
    __table_args__ = (
        CheckConstraint("desired_state IN ('running', 'stopped')", name="ck_appium_nodes_desired_state"),
        CheckConstraint(
            "desired_state = 'running' OR desired_port IS NULL",
            name="ck_appium_nodes_desired_port_requires_running",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    grid_url: Mapped[str] = mapped_column(String, nullable=False)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    container_id: Mapped[str | None] = mapped_column(String, nullable=True)
    active_connection_target: Mapped[str | None] = mapped_column(String, nullable=True)
    # DB constraints keep desired_state to running/stopped and require stopped intent to have no desired_port.
    desired_state: Mapped[NodeState] = mapped_column(
        Enum(NodeState, name="nodestate", create_type=False),
        nullable=False,
        default=NodeState.stopped,
        server_default=NodeState.stopped.value,
    )
    desired_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    desired_grid_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    transition_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    transition_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grid_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
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

    @property
    def observed_running(self) -> bool:
        return self.pid is not None and self.active_connection_target is not None

    @property
    def state(self) -> NodeState:
        """Deprecated object-level compatibility shim for legacy tests and callers.

        App code must use observed columns, health fields, or API effective_state
        instead; CI rejects new ``.state =`` assignments outside tests.
        """
        if self.health_state == NodeState.error.value:
            return NodeState.error
        return NodeState.running if self.observed_running else NodeState.stopped

    @state.setter
    def state(self, value: NodeState | str) -> None:
        state = NodeState(value)
        if state == NodeState.running:
            if self.pid is None:
                self.pid = 0
            if self.active_connection_target is None:
                self.active_connection_target = ""
            self.health_running = None
            self.health_state = None
            return
        if state == NodeState.error:
            self.health_running = False
            self.health_state = NodeState.error.value
            return
        self.pid = None
        self.active_connection_target = None
        self.health_running = None
        self.health_state = None

    device: Mapped[Any] = relationship("Device", back_populates="appium_node")
    resource_claims: Mapped[list[Any]] = relationship(
        "AppiumNodeResourceClaim",
        back_populates="node",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
