from __future__ import annotations

import enum
import uuid
from datetime import datetime  # noqa: TC003 - SQLAlchemy resolves mapped annotations at runtime.
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.devices.models import Device


class GroupType(enum.StrEnum):
    static = "static"
    dynamic = "dynamic"


class DeviceGroup(Base):
    __tablename__ = "device_groups"
    __table_args__ = (Index("ix_device_groups_filters_gin", "filters", postgresql_using="gin"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    group_type: Mapped[GroupType] = mapped_column(Enum(GroupType), default=GroupType.static, nullable=False)
    filters: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    memberships: Mapped[list[DeviceGroupMembership]] = relationship(
        "DeviceGroupMembership", back_populates="group", cascade="all, delete-orphan"
    )


class DeviceGroupMembership(Base):
    __tablename__ = "device_group_memberships"
    __table_args__ = (UniqueConstraint("group_id", "device_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    group: Mapped[DeviceGroup] = relationship("DeviceGroup", back_populates="memberships")
    device: Mapped[Device] = relationship("Device")
