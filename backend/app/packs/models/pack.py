from __future__ import annotations

import enum
import uuid
from datetime import datetime  # noqa: TC003 - SQLAlchemy resolves Mapped annotations at runtime.
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class PackState(enum.StrEnum):
    draft = "draft"
    enabled = "enabled"
    draining = "draining"
    disabled = "disabled"


class DriverPack(Base):
    __tablename__ = "driver_packs"
    __table_args__ = (
        CheckConstraint(
            "origin = 'uploaded'",
            name="driver_packs_origin_ck",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    origin: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    maintainer: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    license: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    current_release: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[PackState] = mapped_column(
        Enum(PackState, native_enum=False),
        default=PackState.enabled,
        server_default="enabled",
    )
    runtime_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: {"strategy": "recommended"},
        server_default='{"strategy": "recommended"}',
    )

    @property
    def is_runnable(self) -> bool:
        return self.state == PackState.enabled

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    releases: Mapped[list[DriverPackRelease]] = relationship(
        "DriverPackRelease", back_populates="pack", cascade="all, delete-orphan"
    )
    host_pack_installations: Mapped[list[Any]] = relationship(
        "HostPackInstallation", back_populates="pack", cascade="all, delete-orphan"
    )


class DriverPackRelease(Base):
    __tablename__ = "driver_pack_releases"
    __table_args__ = (UniqueConstraint("pack_id", "release", name="driver_pack_releases_pack_release_uq"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pack_id: Mapped[str] = mapped_column(String, ForeignKey("driver_packs.id", ondelete="CASCADE"), nullable=False)
    release: Mapped[str] = mapped_column(String, nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    artifact_path: Mapped[str | None] = mapped_column(String, nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    derived_from_pack_id: Mapped[str | None] = mapped_column(String, nullable=True)
    derived_from_release: Mapped[str | None] = mapped_column(String, nullable=True)
    template_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    pack: Mapped[DriverPack] = relationship("DriverPack", back_populates="releases")
    platforms: Mapped[list[DriverPackPlatform]] = relationship(
        "DriverPackPlatform", back_populates="release", cascade="all, delete-orphan"
    )
    features: Mapped[list[DriverPackFeature]] = relationship(
        "DriverPackFeature", back_populates="release", cascade="all, delete-orphan"
    )


class DriverPackPlatform(Base):
    __tablename__ = "driver_pack_platforms"
    __table_args__ = (
        UniqueConstraint(
            "pack_release_id",
            "manifest_platform_id",
            name="driver_pack_platforms_release_platform_uq",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pack_release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("driver_pack_releases.id", ondelete="CASCADE"),
        nullable=False,
    )
    manifest_platform_id: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    automation_name: Mapped[str] = mapped_column(String, nullable=False)
    appium_platform_name: Mapped[str] = mapped_column(String, nullable=False)
    device_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    connection_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    grid_slots: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    release: Mapped[DriverPackRelease] = relationship("DriverPackRelease", back_populates="platforms")


class DriverPackFeature(Base):
    __tablename__ = "driver_pack_features"
    __table_args__ = (
        UniqueConstraint(
            "pack_release_id",
            "manifest_feature_id",
            name="driver_pack_features_release_feature_uq",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pack_release_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("driver_pack_releases.id", ondelete="CASCADE"),
        nullable=False,
    )
    manifest_feature_id: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    release: Mapped[DriverPackRelease] = relationship("DriverPackRelease", back_populates="features")
