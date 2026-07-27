"""Packs domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.devices.services.serialization_types import DeviceReadProjection
    from app.hosts.schemas import DiscoveryConfirmResult, DiscoveryResult, IntakeCandidateRead
    from app.hosts.service import HostTarget


class PackDiscoveryProtocol(Protocol):
    """Discovery split by side effect: one agent dial, then transaction-local reads/writes.

    ``fetch_pack_candidates`` is the only method that touches the network and it
    takes no session, so no caller can hold a transaction across the dial.
    """

    async def fetch_pack_candidates(self, target: HostTarget) -> tuple[Mapping[str, Any], ...]: ...
    async def classify_discovery(
        self, db: AsyncSession, host_id: uuid.UUID, candidates: Sequence[Mapping[str, Any]]
    ) -> DiscoveryResult: ...
    async def build_intake_candidates(
        self, db: AsyncSession, host_id: uuid.UUID, candidates: Sequence[Mapping[str, Any]]
    ) -> list[IntakeCandidateRead]: ...
    async def apply_pack_device_properties(
        self, session: AsyncSession, device: Device, data: dict[str, object]
    ) -> None: ...
    async def confirm_discovery(
        self,
        db: AsyncSession,
        target: HostTarget,
        candidates: Sequence[Mapping[str, Any]],
        add_identity_values: list[str],
        remove_identity_values: list[str],
    ) -> DiscoveryConfirmResult: ...


class DeviceSerializer(Protocol):
    async def serialize_device(self, db: AsyncSession, device: Device) -> dict[str, Any]: ...
    def serialize_projected_device(self, device: Device, projection: DeviceReadProjection) -> dict[str, Any]: ...


class DeviceIdentityGuard(Protocol):
    async def ensure_device_payload_identity_available(
        self,
        db: AsyncSession,
        payload: Mapping[str, Any],
        *,
        exclude_device_id: uuid.UUID | None = ...,
    ) -> None: ...
