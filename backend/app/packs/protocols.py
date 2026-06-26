"""Packs domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.hosts.models import Host
    from app.hosts.schemas import DiscoveryConfirmResult, DiscoveryResult, IntakeCandidateRead


class FeatureStatusRecorder(Protocol):
    async def record_feature_status(
        self, db: AsyncSession, *, host_id: uuid.UUID, pack_id: str, feature_id: str, ok: bool, detail: str
    ) -> bool: ...


class PackDiscoveryProtocol(Protocol):
    async def list_intake_candidates(self, session: AsyncSession, host: Host) -> list[IntakeCandidateRead]: ...
    async def discover_devices(self, session: AsyncSession, host: Host) -> DiscoveryResult: ...
    async def fetch_pack_device_properties(self, host: Host, device: Device) -> dict[str, object] | None: ...
    async def apply_pack_device_properties(
        self, session: AsyncSession, device: Device, data: dict[str, object]
    ) -> None: ...
    async def confirm_discovery(
        self,
        db: AsyncSession,
        host: Host,
        add_identity_values: list[str],
        remove_identity_values: list[str],
        discovery_result: DiscoveryResult,
    ) -> DiscoveryConfirmResult: ...


class DeviceSerializer(Protocol):
    async def serialize_device(self, db: AsyncSession, device: Device) -> dict[str, Any]: ...


class DeviceIdentityGuard(Protocol):
    async def ensure_device_payload_identity_available(
        self,
        db: AsyncSession,
        payload: Mapping[str, Any],
        *,
        exclude_device_id: uuid.UUID | None = ...,
    ) -> None: ...
