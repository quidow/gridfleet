from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from agent_app.pack.adapter_dispatch import (
    adapter_supports,
    declared_adapter_hooks,
    dispatch_doctor,
    missing_declared_hooks,
)
from agent_app.pack.contexts import DoctorCtx
from agent_app.pack.manifest import DesiredPack, parse_desired_payload
from agent_app.pack.runtime_policy import resolve_runtime_spec

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_app.pack.adapter_registry import AdapterRegistry
    from agent_app.pack.adapter_types import DoctorCheckResult
    from agent_app.pack.host_identity import HostIdentity
    from agent_app.pack.manifest import DesiredPayload
    from agent_app.pack.runtime import RuntimeEnv, RuntimeSpec
    from agent_app.pack.runtime_registry import RuntimeRegistry

logger = logging.getLogger(__name__)


class PackStateClient(Protocol):
    async def fetch_desired(self) -> dict[str, Any]:
        raise NotImplementedError


class RuntimeMgr(Protocol):
    async def reconcile(self, desired_by_pack: dict[str, RuntimeSpec]) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
        raise NotImplementedError


class AdapterLoaderFn(Protocol):
    async def __call__(self, pack: DesiredPack, env: RuntimeEnv) -> None:
        raise NotImplementedError


@dataclass
class PackStateLoop:
    client: PackStateClient
    runtime_mgr: RuntimeMgr
    host_identity: HostIdentity
    poll_interval: float = 10.0
    runtime_registry: RuntimeRegistry | None = None
    adapter_registry: AdapterRegistry | None = None
    adapter_loader: AdapterLoaderFn | None = None
    on_status: Callable[[], None] | None = None
    _latest_desired: list[DesiredPack] | None = field(default=None, init=False, repr=False)
    _latest_status: dict[str, Any] | None = field(default=None, init=False, repr=False)

    @property
    def latest_desired_packs(self) -> list[DesiredPack] | None:
        return self._latest_desired

    def latest_status(self) -> dict[str, Any] | None:
        return self._latest_status

    def _resolve_host_id(self) -> str:
        host_id = self.host_identity.get()
        if host_id is None:
            raise RuntimeError("PackStateLoop iteration ran before host identity was assigned")
        return host_id

    async def _resolve_desired_specs(self, parsed: DesiredPayload) -> tuple[dict[str, RuntimeSpec], dict[str, str]]:
        """Resolve a RuntimeSpec per pack; returns (desired_by_pack, resolver_blocked_packs)."""
        resolver_blocked_packs: dict[str, str] = {}
        desired_by_pack: dict[str, RuntimeSpec] = {}
        for pack in parsed.packs:
            resolution = resolve_runtime_spec(
                pack_id=pack.id,
                appium_server=pack.appium_server,
                appium_driver=pack.appium_driver,
            )
            if resolution.error is not None:
                resolver_blocked_packs[pack.id] = resolution.error
                continue
            assert resolution.runtime_spec is not None
            desired_by_pack[pack.id] = dataclasses.replace(
                resolution.runtime_spec,
                runtime_packages=tuple((rp.package, rp.version) for rp in pack.runtime_packages),
            )
        return desired_by_pack, resolver_blocked_packs

    async def run_once(self) -> None:
        host_id = self._resolve_host_id()
        desired_raw = await self.client.fetch_desired()
        parsed = parse_desired_payload(desired_raw)
        self._latest_desired = parsed.packs

        desired_by_pack, resolver_blocked_packs = await self._resolve_desired_specs(parsed)

        prev_runtime_ids: dict[str, str] = {}
        if self.runtime_registry is not None:
            for pack in parsed.packs:
                existing_env = self.runtime_registry.get_for_pack(pack.id)
                if existing_env is not None:
                    prev_runtime_ids[pack.id] = existing_env.runtime_id

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
                        "appium_home": env.appium_home,
                        "status": "installed",
                        "blocked_reason": None,
                    }
                )

            if self.runtime_registry is not None:
                self.runtime_registry.set_for_pack(pack.id, env, release=pack.release)
                runtime_changed = env.runtime_id != prev_runtime_ids.get(pack_id)
                if runtime_changed:
                    doctor_entries.extend(await self._doctor_entries_for_pack(pack, host_id=host_id))

            if (
                self.adapter_loader is not None
                and self.adapter_registry is not None
                and pack.has_adapter_platform
                and not self.adapter_registry.has(pack.id, pack.release)
                and not self.adapter_registry.is_adapterless(pack.id, pack.release)
            ):
                try:
                    await self.adapter_loader(pack, env)
                except Exception as exc:
                    logger.exception("adapter load failed for pack %s@%s", pack.id, pack.release)
                    doctor_entries.append(
                        {
                            "pack_id": pack.id,
                            "check_id": "adapter_load",
                            "ok": False,
                            "message": f"adapter load failed: {exc}",
                        }
                    )

            loaded_adapter = (
                self.adapter_registry.get(pack.id, pack.release) if self.adapter_registry is not None else None
            )
            if (
                loaded_adapter is None
                and self.adapter_registry is not None
                and pack.has_adapter_platform
                and pack.tarball_sha256
                and not self.adapter_registry.is_adapterless(pack.id, pack.release)
            ):
                # The pack ships an adapter but its worker is absent after the load
                # attempt above. Node starts for this pack defer until the worker is
                # loaded (pre_session supplies the device connection caps), so an
                # "installed" row would hide why devices never come up. Surface the
                # blocked state; the doctor entry carries the load failure detail.
                pack_entries.append(
                    {
                        "pack_id": pack.id,
                        "pack_release": pack.release,
                        "runtime_id": env.runtime_id,
                        "status": "blocked",
                        "blocked_reason": "adapter_load_failed",
                    }
                )
                continue
            if (
                loaded_adapter is None
                and self.adapter_registry is not None
                and (not pack.tarball_sha256 or self.adapter_registry.is_adapterless(pack.id, pack.release))
            ):
                # The declare-it-then-implement-it rule for packs without an
                # adapter — wheel-less tarballs and artifact-less packs alike: a
                # declared adapter-owned capability can never be dispatched
                # when the pack ships no adapter at all.
                required = declared_adapter_hooks(pack)
                if required:
                    pack_entries.append(
                        {
                            "pack_id": pack.id,
                            "pack_release": pack.release,
                            "runtime_id": env.runtime_id,
                            "status": "blocked",
                            "blocked_reason": (
                                "manifest declares capabilities that require an adapter, "
                                "but the tarball ships none: " + ", ".join(required)
                            ),
                        }
                    )
                    continue
            if loaded_adapter is not None and not getattr(loaded_adapter, "alive", True):
                doctor_entries.append(
                    {
                        "pack_id": pack.id,
                        "check_id": "adapter_load",
                        "ok": False,
                        "message": "adapter worker is unavailable",
                    }
                )
                pack_entries.append(
                    {
                        "pack_id": pack.id,
                        "pack_release": pack.release,
                        "runtime_id": env.runtime_id,
                        "status": "blocked",
                        "blocked_reason": "adapter_worker_unavailable",
                    }
                )
                continue
            if loaded_adapter is not None:
                missing = missing_declared_hooks(pack, loaded_adapter)
                if missing:
                    pack_entries.append(
                        {
                            "pack_id": pack.id,
                            "pack_release": pack.release,
                            "runtime_id": env.runtime_id,
                            "status": "blocked",
                            "blocked_reason": (
                                "manifest declares capabilities the adapter does not implement: " + ", ".join(missing)
                            ),
                        }
                    )
                    continue

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

        if self.runtime_registry is not None:
            # Shed runtimes for packs the backend has retired from the
            # desired list so `resolve_appium_invocation_for_pack` cannot
            # hand out a binary for a pack we no longer manage. Keyed on
            # the desired-pack set (not env_by_pack) so a transient
            # runtime_mgr.reconcile failure does not evict cached envs
            # for packs the backend still wants.
            self.runtime_registry.purge_except({pack.id for pack in parsed.packs})

        if self.adapter_registry is not None:
            desired_keys = {(pack.id, pack.release) for pack in parsed.packs if pack.has_adapter_platform}
            for pack_id, release in self.adapter_registry.keys():  # noqa: SIM118
                if (pack_id, release) in desired_keys:
                    continue
                handle = self.adapter_registry.remove(pack_id, release)
                if handle is not None:
                    await handle.shutdown()
            # Adapterless marks have no worker handle, so the removal loop above
            # never reaps them; a retired release re-uploaded with a wheel must
            # not be silently skipped by a stale mark.
            self.adapter_registry.purge_adapterless_except(desired_keys)

        self._latest_status = {
            "runtimes": runtime_entries,
            "packs": pack_entries,
            "doctor": doctor_entries,
        }
        if self.on_status is not None:
            self.on_status()

    async def _doctor_entries_for_pack(
        self,
        pack: DesiredPack,
        *,
        host_id: str,
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        if self.adapter_registry is not None:
            adapter = self.adapter_registry.get(pack.id, pack.release)
            if adapter is not None and adapter_supports(adapter, "doctor"):
                try:
                    for result in await dispatch_doctor(adapter, DoctorCtx(host_id=host_id)):
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
