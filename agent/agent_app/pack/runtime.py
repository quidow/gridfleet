from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from agent_app.config import agent_settings


@dataclass(frozen=True)
class RuntimeSpec:
    server_package: str
    server_version: str
    drivers: tuple[tuple[str, str, str, str | None], ...]  # (name, version, source, github_repo)
    plugins: tuple[tuple[str, str, str, str | None], ...]  # (name, version, source, package)
    node_major: int | None
    runtime_packages: tuple[tuple[str, str], ...] = ()  # (package, version) extra npm installs


@dataclass
class RuntimeEnv:
    runtime_id: str
    appium_home: str
    appium_bin: str
    server_package: str
    server_version: str
    driver_versions: dict[str, str] = field(default_factory=dict)
    plugin_statuses: list[dict[str, str | None]] = field(default_factory=list)
    runtime_packages: list[list[str]] = field(default_factory=list)  # [[package, version], ...]


class NpmRunner(Protocol):
    async def install_appium(self, package: str, version: str, appium_home: str) -> str:
        raise NotImplementedError

    async def install_package(self, package: str, version: str, appium_home: str) -> None:
        raise NotImplementedError

    async def install_driver(
        self,
        name: str,
        package: str,
        version: str,
        appium_home: str,
        *,
        source: str = "npm",
        github_repo: str | None = None,
    ) -> None:
        raise NotImplementedError

    async def install_plugin(
        self,
        name: str,
        version: str,
        source: str,
        package: str | None,
        appium_home: str,
    ) -> None:
        raise NotImplementedError


class RealNpmRunner:
    async def install_appium(self, package: str, version: str, appium_home: str) -> str:
        Path(appium_home).mkdir(parents=True, exist_ok=True)
        env = {**dict(os.environ), "APPIUM_HOME": appium_home}
        proc = await asyncio.create_subprocess_exec(
            "npm",
            "install",
            "--prefix",
            appium_home,
            "--save-exact",
            f"{package}@{version}",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"appium install failed: {err.decode(errors='replace')}")
        return str(Path(appium_home) / "node_modules" / ".bin" / "appium")

    async def install_package(self, package: str, version: str, appium_home: str) -> None:
        env = {**dict(os.environ), "APPIUM_HOME": appium_home}
        proc = await asyncio.create_subprocess_exec(
            "npm",
            "install",
            "--prefix",
            appium_home,
            "--save-exact",
            f"{package}@{version}",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"runtime package install failed: {err.decode(errors='replace')}")

    async def install_driver(
        self,
        name: str,
        package: str,
        version: str,
        appium_home: str,
        *,
        source: str = "npm",
        github_repo: str | None = None,
    ) -> None:
        env = {**dict(os.environ), "APPIUM_HOME": appium_home}
        appium_bin = str(Path(appium_home) / "node_modules" / ".bin" / "appium")
        for cmd in _driver_install_commands(appium_bin, appium_home, package, version, source, github_repo):
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            if proc.returncode != 0:
                output = "\n".join(
                    part
                    for part in (out.decode(errors="replace").strip(), err.decode(errors="replace").strip())
                    if part
                )
                raise RuntimeError(f"driver install failed: {output}")

    async def install_plugin(
        self,
        name: str,
        version: str,
        source: str,
        package: str | None,
        appium_home: str,
    ) -> None:
        env = {**dict(os.environ), "APPIUM_HOME": appium_home}
        appium_bin = str(Path(appium_home) / "node_modules" / ".bin" / "appium")
        cmd = _plugin_install_command(appium_bin, name, version, source, package)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(err.decode(errors="replace"))


def _is_driver_already_installed_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "driver named" in message and "already installed" in message


def _versioned(value: str, version: str) -> str:
    return value if "@" in value.rsplit("/", 1)[-1] else f"{value}@{version}"


def _github_ref(version: str) -> str:
    return f"v{version}" if re.match(r"^\d+\.\d+\.\d+", version) else version


def _github_npm_install_spec(github_repo: str, version: str) -> str:
    base, separator, explicit_ref = github_repo.removeprefix("github:").partition("#")
    ref = explicit_ref if separator else _github_ref(version)
    if base.startswith("git+"):
        install_spec = base
    elif base.startswith("https://github.com/"):
        install_spec = f"git+{base}"
    else:
        install_spec = f"git+https://github.com/{base}"
    if not install_spec.endswith(".git"):
        install_spec = f"{install_spec}.git"
    return f"{install_spec}#{ref}" if ref else install_spec


def _installed_package_version(appium_home: str, package: str) -> str | None:
    package_json = Path(appium_home) / "node_modules" / package / "package.json"
    try:
        data = json.loads(package_json.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    version = data.get("version")
    return version if isinstance(version, str) and version else None


def _driver_install_commands(
    appium_bin: str,
    appium_home: str,
    package: str,
    version: str,
    source: str,
    github_repo: str | None,
) -> list[list[str]]:
    if source == "github":
        if not github_repo:
            raise ValueError("github_repo required for source=github")
        return [
            [
                "npm",
                "install",
                "--prefix",
                appium_home,
                "--save-dev",
                "--no-progress",
                "--no-audit",
                _github_npm_install_spec(github_repo, version),
            ],
            [appium_bin, "driver", "list", "--installed"],
        ]
    return [[appium_bin, "driver", "install", f"--source={source}", f"{package}@{version}"]]


def _plugin_install_command(appium: str, name: str, version: str, source: str, package: str | None) -> list[str]:
    if source.startswith("npm:"):
        package_name = source.removeprefix("npm:")
        return [appium, "plugin", "install", _versioned(package_name, version), "--source=npm"]
    if source.startswith("github:"):
        install_spec = source.removeprefix("github:")
        cmd = [appium, "plugin", "install", install_spec, "--source=github"]
    elif source.startswith("git:"):
        install_spec = source.removeprefix("git:")
        cmd = [appium, "plugin", "install", install_spec, "--source=git"]
    elif source.startswith("local:"):
        install_spec = source.removeprefix("local:")
        cmd = [appium, "plugin", "install", install_spec, "--source=local"]
    else:
        return [appium, "plugin", "install", _versioned(name, version)]
    if package:
        cmd.append(f"--package={package}")
    return cmd


_RUNTIME_COMPLETE_MARKER = ".runtime-complete"


def _write_runtime_marker(env: RuntimeEnv) -> None:
    """Record a successful install. Written LAST so a partial/crashed install
    never carries the marker and falls through to a clean reinstall."""
    home = Path(env.appium_home)
    home.mkdir(parents=True, exist_ok=True)
    (home / _RUNTIME_COMPLETE_MARKER).write_text(json.dumps(asdict(env)))


def _adopt_runtime_from_disk(rid: str, appium_home: str) -> RuntimeEnv | None:
    """Rebuild a RuntimeEnv from a completed install left by a previous agent
    process. The dir name is the spec hash, so contents match the spec by
    construction. Returns None (→ reinstall) on any validation failure."""
    try:
        data = json.loads((Path(appium_home) / _RUNTIME_COMPLETE_MARKER).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    appium_bin = data.get("appium_bin")
    if not isinstance(appium_bin, str) or not Path(appium_bin).is_file() or not os.access(appium_bin, os.X_OK):
        return None
    server_package = data.get("server_package")
    server_version = data.get("server_version")
    driver_versions = data.get("driver_versions")
    plugin_statuses = data.get("plugin_statuses")
    if not isinstance(server_package, str) or not isinstance(server_version, str):
        return None
    if not isinstance(driver_versions, dict) or not isinstance(plugin_statuses, list):
        return None
    runtime_packages = data.get("runtime_packages") or []
    if not isinstance(runtime_packages, list):
        return None
    # A declared runtime package missing on disk means an incomplete install; do
    # NOT adopt it (else it sticks forever) — fall through to a clean reinstall.
    node_modules = Path(appium_home) / "node_modules"
    for entry in runtime_packages:
        if not (isinstance(entry, list) and len(entry) == 2 and (node_modules / str(entry[0])).is_dir()):
            return None
    return RuntimeEnv(
        runtime_id=rid,
        appium_home=appium_home,
        appium_bin=appium_bin,
        server_package=server_package,
        server_version=server_version,
        driver_versions=driver_versions,
        plugin_statuses=plugin_statuses,
        runtime_packages=runtime_packages,
    )


class AppiumRuntimeManager:
    def __init__(self, runner: NpmRunner | None = None, root_dir: Path | None = None) -> None:
        self._runner = runner or RealNpmRunner()
        if root_dir is not None:
            self._root = root_dir
        else:
            self._root = Path(agent_settings.runtime.runtime_root)
        self._refcounts: dict[str, int] = {}
        self._installed: dict[str, RuntimeEnv] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def runtime_id_for(spec: RuntimeSpec) -> str:
        payload = {
            "server": f"{spec.server_package}@{spec.server_version}",
            "drivers": sorted([f"{n}@{v}:{s}:{g}" for n, v, s, g in spec.drivers]),
            "plugins": sorted([f"{n}@{v}:{s}:{p}" for n, v, s, p in spec.plugins]),
            "runtime_packages": sorted([f"{p}@{v}" for p, v in spec.runtime_packages]),
            "node_major": spec.node_major,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    def refcount(self, runtime_id: str) -> int:
        return self._refcounts.get(runtime_id, 0)

    async def reconcile(self, desired_by_pack: dict[str, RuntimeSpec]) -> tuple[dict[str, RuntimeEnv], dict[str, str]]:
        async with self._lock:
            pack_to_runtime: dict[str, str] = {}
            specs_by_rid: dict[str, RuntimeSpec] = {}
            for pack_id, spec in desired_by_pack.items():
                rid = self.runtime_id_for(spec)
                pack_to_runtime[pack_id] = rid
                specs_by_rid[rid] = spec

            failed_rids: dict[str, str] = {}
            for rid, spec in specs_by_rid.items():
                if rid in self._installed:
                    continue
                appium_home = str(self._root / rid)
                adopted = _adopt_runtime_from_disk(rid, appium_home)
                if adopted is not None:
                    self._installed[rid] = adopted
                    continue
                try:
                    bin_path = await self._runner.install_appium(spec.server_package, spec.server_version, appium_home)
                    for drv_name, drv_version, drv_source, drv_github_repo in spec.drivers:
                        try:
                            await self._runner.install_driver(
                                drv_name,
                                drv_name,
                                drv_version,
                                appium_home,
                                source=drv_source,
                                github_repo=drv_github_repo,
                            )
                        except Exception as exc:
                            if not _is_driver_already_installed_error(exc):
                                raise
                    # Install manifest-declared extra packages explicitly. npm may
                    # silently skip a driver's optional deps in this headless install
                    # env; an explicit install can't be skipped (it installs or errors).
                    for pkg_name, pkg_version in spec.runtime_packages:
                        await self._runner.install_package(pkg_name, pkg_version, appium_home)
                    plugin_statuses: list[dict[str, str | None]] = []
                    for name, version, source, package in spec.plugins:
                        try:
                            await self._runner.install_plugin(name, version, source, package, appium_home)
                        except Exception as exc:
                            plugin_statuses.append(
                                {
                                    "name": name,
                                    "version": version,
                                    "source": source,
                                    "package": package,
                                    "status": "blocked",
                                    "blocked_reason": f"plugin_install_failed: {exc}",
                                }
                            )
                        else:
                            plugin_statuses.append(
                                {
                                    "name": name,
                                    "version": version,
                                    "source": source,
                                    "package": package,
                                    "status": "installed",
                                    "blocked_reason": None,
                                }
                            )
                    env = RuntimeEnv(
                        runtime_id=rid,
                        appium_home=appium_home,
                        appium_bin=bin_path,
                        server_package=spec.server_package,
                        server_version=_installed_package_version(appium_home, spec.server_package)
                        or spec.server_version,
                        driver_versions={
                            drv_name: _installed_package_version(appium_home, drv_name) or drv_version
                            for drv_name, drv_version, _drv_source, _drv_github_repo in spec.drivers
                        },
                        plugin_statuses=plugin_statuses,
                        runtime_packages=[[pkg_name, pkg_version] for pkg_name, pkg_version in spec.runtime_packages],
                    )
                    _write_runtime_marker(env)
                    self._installed[rid] = env
                except Exception as exc:
                    failed_rids[rid] = f"rid={rid} appium_home={appium_home}: {exc}"

            # Build per-pack errors: all packs sharing a failed rid get the error message.
            errors_by_pack: dict[str, str] = {}
            for pack_id, rid in pack_to_runtime.items():
                if rid in failed_rids:
                    errors_by_pack[pack_id] = failed_rids[rid]

            # Refcounts: only count refs for successfully installed rids so failed
            # rids are retried on the next reconcile (missing from _installed + _refcounts).
            new_refcounts: dict[str, int] = {}
            for _pack_id, rid in pack_to_runtime.items():
                if rid in self._installed:
                    new_refcounts[rid] = new_refcounts.get(rid, 0) + 1
            for rid in self._installed:
                new_refcounts.setdefault(rid, 0)
            self._refcounts = new_refcounts

            envs = {pack_id: self._installed[rid] for pack_id, rid in pack_to_runtime.items() if rid in self._installed}
            return envs, errors_by_pack
