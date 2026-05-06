from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from agent_app.pack.adapter_dispatch import dispatch_doctor
from agent_app.pack.manifest import DesiredPack, parse_desired_payload
from agent_app.pack.runtime_policy import resolve_runtime_spec

if TYPE_CHECKING:
    from agent_app.pack.adapter_registry import AdapterRegistry
    from agent_app.pack.adapter_types import DoctorCheckResult
    from agent_app.pack.runtime import RuntimeEnv, RuntimeSpec
    from agent_app.pack.runtime_registry import RuntimeRegistry
    from agent_app.pack.sidecar_supervisor import SidecarSupervisor

logger = logging.getLogger(__name__)


class PackStateClient(Protocol):
    async def fetch_desired(self) -> dict[str, Any]:
        pass

    async def post_status(self, payload: dict[str, Any]) -> None:
        pass


class RuntimeMgr(Protocol):
    async def reconcile(self, desired_by_pack: dict[str, RuntimeSpec]) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
        pass


class AdapterLoaderFn(Protocol):
    async def __call__(self, pack: DesiredPack, env: RuntimeEnv) -> None:
        pass


class VersionCatalog(Protocol):
    async def versions(self, package: str) -> list[str]:
        pass


class DriverDoctorRunner(Protocol):
    async def __call__(self, driver_name: str, env: RuntimeEnv) -> dict[str, object]:
        pass


@dataclass
class _DoctorCtx:
    host_id: str


@dataclass
class PackStateLoop:
    client: PackStateClient
    runtime_mgr: RuntimeMgr
    host_id: str
    poll_interval: float = 10.0
    runtime_registry: RuntimeRegistry | None = None
    adapter_registry: AdapterRegistry | None = None
    adapter_loader: AdapterLoaderFn | None = None
    sidecar_supervisor: SidecarSupervisor | None = None
    version_catalog: VersionCatalog | None = None
    driver_doctor_runner: DriverDoctorRunner | None = None
    _latest_desired: list[DesiredPack] | None = field(default=None, init=False, repr=False)

    @property
    def latest_desired_packs(self) -> list[DesiredPack] | None:
        return self._latest_desired

    async def run_once(self) -> None:
        desired_raw = await self.client.fetch_desired()
        parsed = parse_desired_payload(desired_raw)
        self._latest_desired = parsed.packs

        resolver_blocked_packs: dict[str, str] = {}
        desired_by_pack: dict[str, RuntimeSpec] = {}
        for pack in parsed.packs:
            available_versions: dict[str, list[str]] = {}
            if pack.runtime_policy.strategy == "latest_patch":
                for installable in (pack.appium_server, pack.appium_driver):
                    if installable.source != "npm":
                        continue
                    if installable.available_versions:
                        available_versions[installable.package] = list(installable.available_versions)
                    elif self.version_catalog is not None:
                        available_versions[installable.package] = await self.version_catalog.versions(
                            installable.package
                        )
            resolution = resolve_runtime_spec(
                pack_id=pack.id,
                appium_server=pack.appium_server,
                appium_driver=pack.appium_driver,
                policy=pack.runtime_policy,
                available_versions=available_versions,
            )
            if resolution.error is not None:
                resolver_blocked_packs[pack.id] = resolution.error
                continue
            assert resolution.runtime_spec is not None
            desired_by_pack[pack.id] = dataclasses.replace(
                resolution.runtime_spec,
                plugins=tuple((p.name, p.version, p.source, p.package) for p in parsed.plugins),
            )

        try:
            env_by_pack, errors_by_pack = await self.runtime_mgr.reconcile(desired_by_pack)
        except Exception:
            logger.exception("runtime reconcile failed")
            env_by_pack = {}
            errors_by_pack = {}

        runtime_entries: list[dict[str, Any]] = []
        seen_runtime_ids: set[str] = set()
        pack_entries: list[dict[str, Any]] = []
        doctor_entries: list[dict[str, Any]] = []
        pack_by_id = {pack.id: pack for pack in parsed.packs}

        for pack_id, pack in pack_by_id.items():
            if pack_id in resolver_blocked_packs:
                pack_entries.append(
                    {
                        "pack_id": pack.id,
                        "pack_release": pack.release,
                        "runtime_id": None,
                        "status": "blocked",
                        "blocked_reason": resolver_blocked_packs[pack_id],
                    }
                )
                continue

            spec = desired_by_pack[pack_id]
            env = env_by_pack.get(pack_id)
            if env is None:
                blocked_reason = errors_by_pack.get(pack_id, "runtime_install_failed")
                pack_entries.append(
                    {
                        "pack_id": pack.id,
                        "pack_release": pack.release,
                        "runtime_id": None,
                        "status": "blocked",
                        "blocked_reason": blocked_reason,
                    }
                )
                continue

            if env.runtime_id not in seen_runtime_ids:
                seen_runtime_ids.add(env.runtime_id)
                runtime_entries.append(
                    {
                        "runtime_id": env.runtime_id,
                        "appium_server": {
                            "package": env.server_package,
                            "version": env.server_version,
                        },
                        "appium_driver": [
                            {"package": n, "version": env.driver_versions.get(n, v)} for n, v, _s, _g in spec.drivers
                        ],
                        "appium_plugins": env.plugin_statuses,
                        "appium_home": env.appium_home,
                        "status": "installed",
                        "blocked_reason": None,
                    }
                )

            if self.runtime_registry is not None:
                self.runtime_registry.set_for_pack(pack.id, env)

            if (
                self.adapter_loader is not None
                and self.adapter_registry is not None
                and pack.has_adapter_platform
                and not self.adapter_registry.has(pack.id, pack.release)
            ):
                try:
                    await self.adapter_loader(pack, env)
                except Exception:
                    logger.exception("adapter load failed for pack %s@%s", pack.id, pack.release)

            if self.sidecar_supervisor is not None and self.adapter_registry is not None:
                adapter = self.adapter_registry.get(pack.id, pack.release)
                if adapter is not None:
                    for feature_id in pack.sidecar_feature_ids:
                        await self.sidecar_supervisor.start(
                            pack_id=pack.id,
                            release=pack.release,
                            feature_id=feature_id,
                            adapter=adapter,
                        )

            doctor_entries.extend(await self._doctor_entries_for_pack(pack, env, spec))

            pack_entries.append(
                {
                    "pack_id": pack.id,
                    "pack_release": pack.release,
                    "runtime_id": env.runtime_id,
                    "status": "installed",
                    "resolved_install_spec": {
                        "appium_server": f"{env.server_package}@{env.server_version}",
                        "appium_driver": {n: v for n, v, _s, _g in spec.drivers},
                    },
                    "installer_log_excerpt": "",
                    "resolver_version": "1",
                    "blocked_reason": None,
                }
            )

        desired_sidecars = {
            (pack.id, pack.release, feature_id)
            for pack in parsed.packs
            for feature_id in pack.sidecar_feature_ids
            if pack.id in env_by_pack
        }
        if self.sidecar_supervisor is not None and self.adapter_registry is not None:
            stale_sidecars = sorted(self.sidecar_supervisor.tracked_keys() - desired_sidecars)
            for pack_id, release, feature_id in stale_sidecars:
                adapter = self.adapter_registry.get(pack_id, release)
                if adapter is not None:
                    await self.sidecar_supervisor.stop(
                        pack_id=pack_id,
                        release=release,
                        feature_id=feature_id,
                        adapter=adapter,
                    )
                else:
                    await self.sidecar_supervisor.drop(
                        pack_id=pack_id,
                        release=release,
                        feature_id=feature_id,
                    )

        sidecars: list[dict[str, Any]] = (
            self.sidecar_supervisor.status_snapshot() if self.sidecar_supervisor is not None else []
        )
        payload = {
            "host_id": self.host_id,
            "runtimes": runtime_entries,
            "packs": pack_entries,
            "doctor": doctor_entries,
            "sidecars": sidecars,
        }
        await self.client.post_status(payload)

    async def _doctor_entries_for_pack(
        self,
        pack: DesiredPack,
        env: RuntimeEnv,
        spec: RuntimeSpec,
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        if self.adapter_registry is not None:
            adapter = self.adapter_registry.get(pack.id, pack.release)
            if adapter is not None:
                try:
                    for result in await dispatch_doctor(adapter, _DoctorCtx(host_id=self.host_id)):
                        entries.append(_adapter_doctor_entry(pack.id, result))
                except Exception as exc:
                    logger.exception("adapter doctor failed for pack %s@%s", pack.id, pack.release)
                    entries.append(
                        {
                            "pack_id": pack.id,
                            "check_id": "adapter_doctor",
                            "ok": False,
                            "message": str(exc),
                        }
                    )

        if self.driver_doctor_runner is not None:
            for driver_package, _version, _source, _github_repo in spec.drivers:
                driver_name = _driver_name_from_package(driver_package)
                driver_result = await self.driver_doctor_runner(driver_name, env)
                entries.append(_driver_doctor_entry(pack.id, driver_result))
        return entries

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("pack state loop iteration failed")
            await asyncio.sleep(self.poll_interval)


def _adapter_doctor_entry(pack_id: str, result: DoctorCheckResult) -> dict[str, Any]:
    return {
        "pack_id": pack_id,
        "check_id": result.check_id,
        "ok": result.ok,
        "message": result.message,
    }


def _driver_doctor_entry(pack_id: str, result: dict[str, object]) -> dict[str, Any]:
    issues = result.get("issues")
    message = "; ".join(item for item in issues if isinstance(item, str)) if isinstance(issues, list) else ""
    return {
        "pack_id": pack_id,
        "check_id": "driver",
        "ok": bool(result.get("ok")),
        "message": message,
    }


def _driver_name_from_package(package: str) -> str:
    bare_package = package.rsplit("/", 1)[-1]
    if bare_package.startswith("appium-"):
        bare_package = bare_package.removeprefix("appium-")
    return bare_package.removesuffix("-driver")
