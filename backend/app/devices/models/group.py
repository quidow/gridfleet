from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    func,
    text,
)
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
    __table_args__ = (
        Index("ix_device_groups_filters_gin", "filters", postgresql_using="gin"),
        Index("ix_device_groups_key", "key", unique=True),
        # Redundant with the PK on ``id`` alone, but a composite FK can only
        # target a declared unique key: ``DeviceGroupMemberOf`` references
        # ``(id, group_type)`` so the database pins each endpoint's kind.
        UniqueConstraint("id", "group_type", name="uq_device_groups_id_group_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    group_type: Mapped[GroupType] = mapped_column(Enum(GroupType), default=GroupType.static, nullable=False)
    filters: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    memberships: Mapped[list[DeviceGroupMembership]] = relationship(
        "DeviceGroupMembership", back_populates="group", cascade="all, delete-orphan", passive_deletes=True
    )


class DeviceGroupMemberOf(Base):
    """One dynamic group's reference to one static group it filters on.

    Replaces the ``filters.member_of`` JSON array. No ``relationship()`` is
    declared on either side on purpose: the definition loader joins explicitly,
    ``delete_group`` deletes with Core SQL, and both ``ON DELETE`` rules run in
    PostgreSQL — so a relationship pair would have no reader and would only
    widen the surface ``raiseload("*")`` guards.

    Constraint names must stay character-identical to revision ``6d8c3b5042b5``:
    the test suite builds its schema from ``Base.metadata.create_all`` while
    production is migrated, so a divergence means the two carry different
    constraints. The three CHECKs are declared with short names because the
    metadata's ``ck`` naming convention rewraps them as
    ``{table_name}_{name}_check``.
    """

    __tablename__ = "device_group_member_of"
    __table_args__ = (
        # Named explicitly: the metadata's "pk" convention is "%(table_name)s_pkey"
        # and does not reference %(constraint_name)s, so an unnamed composite PK
        # would land as ``device_group_member_of_pkey`` here while the migration
        # created ``pk_device_group_member_of``.
        PrimaryKeyConstraint("dynamic_group_id", "static_group_id", name="pk_device_group_member_of"),
        CheckConstraint("dynamic_group_id <> static_group_id", name="not_self"),
        # The composite FKs only prove the supplied ``(id, group_type)`` pair
        # exists; they never pin a column to a literal. Without these two checks
        # an insert could name a static group's genuine ``(id, 'static')`` pair
        # as the dynamic endpoint and still satisfy the FK.
        CheckConstraint("dynamic_group_type = 'dynamic'", name="dynamic_type"),
        CheckConstraint("static_group_type = 'static'", name="static_type"),
        ForeignKeyConstraint(
            ["dynamic_group_id", "dynamic_group_type"],
            ["device_groups.id", "device_groups.group_type"],
            name="fk_device_group_member_of_dynamic_group",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["static_group_id", "static_group_type"],
            ["device_groups.id", "device_groups.group_type"],
            name="fk_device_group_member_of_static_group",
            ondelete="RESTRICT",
        ),
        Index("ix_device_group_member_of_static_group_id", "static_group_id"),
    )

    dynamic_group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    dynamic_group_type: Mapped[GroupType] = mapped_column(
        Enum(GroupType), nullable=False, default=GroupType.dynamic, server_default=text("'dynamic'::grouptype")
    )
    static_group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    static_group_type: Mapped[GroupType] = mapped_column(
        Enum(GroupType), nullable=False, default=GroupType.static, server_default=text("'static'::grouptype")
    )


class DeviceGroupMembership(Base):
    __tablename__ = "device_group_memberships"
    __table_args__ = (UniqueConstraint("group_id", "device_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )

    group: Mapped[DeviceGroup] = relationship("DeviceGroup", back_populates="memberships")
    device: Mapped["Device"] = relationship("Device")
