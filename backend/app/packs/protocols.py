"""Packs domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import uuid

    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.hosts.models import Host
    from app.hosts.schemas import DiscoveryConfirmResult, DiscoveryResult, IntakeCandidateRead
    from app.packs.adapter import FeatureActionResult
    from app.packs.models import DriverPack, PackState
    from app.packs.schemas import PackCatalog, PackOut, PackReleasesOut, RuntimePolicy


@runtime_checkable
class PackCatalogProtocol(Protocol):
    async def list_catalog(self, db: AsyncSession) -> PackCatalog: ...
    async def get_pack_detail(self, db: AsyncSession, pack_id: str) -> PackOut | None: ...
    async def set_runtime_policy(self, db: AsyncSession, pack_id: str, policy: RuntimePolicy) -> DriverPack: ...
    async def delete_pack(self, db: AsyncSession, pack_id: str) -> None: ...


@runtime_checkable
class PackReleaseProtocol(Protocol):
    async def list_releases(self, db: AsyncSession, pack_id: str) -> PackReleasesOut | None: ...
    async def delete_release(self, db: AsyncSession, pack_id: str, release: str) -> None: ...
    async def set_current_release(self, db: AsyncSession, pack_id: str, release: str) -> DriverPack: ...
    async def upload(self, db: AsyncSession, *, username: str, origin_filename: str, data: bytes) -> DriverPack: ...
    async def export(self, db: AsyncSession, pack_id: str, release: str) -> tuple[bytes, str]: ...


@runtime_checkable
class FeatureStatusRecorder(Protocol):
    async def record_feature_status(
        self, db: AsyncSession, *, host_id: uuid.UUID, pack_id: str, feature_id: str, ok: bool, detail: str
    ) -> bool: ...


@runtime_checkable
class FeatureProtocol(FeatureStatusRecorder, Protocol):
    async def dispatch_feature_action(
        self,
        db: AsyncSession,
        *,
        host_id: uuid.UUID,
        pack_id: str,
        feature_id: str,
        action_id: str,
        args: dict[str, Any],
        agent_auth: httpx.BasicAuth | None = None,
    ) -> FeatureActionResult: ...


@runtime_checkable
class PackStatusProtocol(Protocol):
    async def apply_status(self, db: AsyncSession, payload: dict[str, Any]) -> None: ...
    async def persist_doctor_results(
        self, db: AsyncSession, host_id: uuid.UUID, pack_id: str, checks: list[dict[str, Any]]
    ) -> None: ...
    async def get_host_driver_pack_status(self, db: AsyncSession, host_id: uuid.UUID) -> dict[str, Any]: ...
    async def get_driver_pack_host_status(self, db: AsyncSession, pack_id: str) -> dict[str, Any]: ...
    async def upsert_plugin_status(
        self,
        db: AsyncSession,
        *,
        host_id: uuid.UUID,
        runtime_id: str,
        plugin_name: str,
        version: str,
        status: str,
        blocked_reason: str | None = None,
    ) -> None: ...
    async def compute_desired(self, db: AsyncSession, host_id: uuid.UUID) -> dict[str, Any]: ...


@runtime_checkable
class PackLifecycleProtocol(Protocol):
    async def count_active_work_for_pack(self, db: AsyncSession, pack_id: str) -> dict[str, int]: ...
    async def try_complete_drain(self, db: AsyncSession, pack_id: str) -> DriverPack: ...
    async def transition_pack_state(
        self, db: AsyncSession, pack_id: str, target: PackState, *, override: bool = False
    ) -> DriverPack: ...


@runtime_checkable
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


@runtime_checkable
class DeviceSerializer(Protocol):
    async def serialize_device(self, db: AsyncSession, device: Device) -> dict[str, Any]: ...
