"""Manage host-level Appium and Selenium Grid tool versions."""

from __future__ import annotations

import asyncio
import contextlib
import glob
import logging
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from packaging.version import InvalidVersion, Version

from agent_app.appium_process import _build_env
from agent_app.capabilities import refresh_capabilities_snapshot
from agent_app.config import agent_settings
from agent_app.tool_paths import _parse_node_version
from agent_app.tool_paths import find_appium as _find_appium

logger = logging.getLogger(__name__)

APPIUM_INSTALL_TIMEOUT_SEC = 180
TOOL_VERSION_TIMEOUT_SEC = 10
SELENIUM_DOWNLOAD_TIMEOUT_SEC = 120
SELENIUM_JAR_URL = (
    "https://github.com/SeleniumHQ/selenium/releases/download/selenium-{version}/selenium-server-{version}.jar"
)


def _normalize_selenium_version(version: str) -> str | None:
    stripped = version.strip()
    parts = stripped.split(".")
    if len(parts) != 3 or any(not part or not all("0" <= char <= "9" for char in part) for part in parts):
        return None
    try:
        parsed = Version(stripped)
    except InvalidVersion:
        return None
    if (
        parsed.epoch != 0
        or parsed.post is not None
        or parsed.is_prerelease
        or parsed.is_devrelease
        or parsed.local is not None
    ):
        return None
    normalized = str(parsed)
    if normalized != stripped:
        return None
    release = parsed.release
    if len(release) != 3:
        return None
    return normalized


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str


@dataclass(frozen=True)
class NodeProvider:
    name: str
    node_path: str | None
    npm_path: str | None
    bin_paths: list[str] = field(default_factory=list)
    command_prefix: list[str] = field(default_factory=list)
    error: str | None = None

    def command(self, executable: str, *args: str) -> list[str]:
        if self.command_prefix:
            return [*self.command_prefix, executable, *args]
        if executable == "node" and self.node_path:
            return [self.node_path, *args]
        if executable == "npm" and self.npm_path:
            return [self.npm_path, *args]
        return [executable, *args]


def _provider_env(provider: NodeProvider | None = None) -> dict[str, str]:
    env = _build_env()
    if provider and provider.bin_paths:
        existing = env.get("PATH", "")
        paths = [path for path in provider.bin_paths if path and path not in existing.split(os.pathsep)]
        if paths:
            env["PATH"] = os.pathsep.join([*paths, existing])
    return env


def _prepend_process_path(paths: list[str]) -> None:
    existing = os.environ.get("PATH", "")
    existing_parts = existing.split(os.pathsep) if existing else []
    new_paths = [path for path in paths if path and path not in existing_parts]
    if new_paths:
        os.environ["PATH"] = os.pathsep.join([*new_paths, existing])


async def _run_command(
    cmd: list[str],
    *,
    timeout: int = TOOL_VERSION_TIMEOUT_SEC,
    env: dict[str, str] | None = None,
) -> CommandResult:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    output = stdout.decode(errors="replace").strip() or stderr.decode(errors="replace").strip()
    return CommandResult(proc.returncode or 0, output)


async def _run_optional(
    cmd: list[str],
    *,
    timeout: int = TOOL_VERSION_TIMEOUT_SEC,
    env: dict[str, str] | None = None,
) -> CommandResult | None:
    try:
        return await _run_command(cmd, timeout=timeout, env=env)
    except (FileNotFoundError, TimeoutError, OSError):
        return None


def _first_version(output: str) -> str | None:
    match = re.search(r"v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)", output)
    return match.group(1) if match else None


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _find_fnm_binary() -> str | None:
    found = shutil.which("fnm")
    if found:
        return found
    for path in [
        "/usr/local/bin/fnm",
        os.path.expanduser("~/.local/bin/fnm"),
        os.path.expanduser("~/Library/Application Support/fnm/fnm"),
    ]:
        if _is_executable(path):
            return path
    return None


def _fnm_base_dirs() -> list[str]:
    dirs: list[str] = []
    if os.getenv("FNM_DIR"):
        dirs.append(os.path.expanduser(os.environ["FNM_DIR"]))
    if os.getenv("XDG_DATA_HOME"):
        dirs.append(os.path.join(os.path.expanduser(os.environ["XDG_DATA_HOME"]), "fnm"))
    dirs.extend(
        [
            os.path.expanduser("~/.local/share/fnm"),
            os.path.expanduser("~/Library/Application Support/fnm"),
        ]
    )
    unique: list[str] = []
    for path in dirs:
        if path not in unique:
            unique.append(path)
    return unique


def _fnm_default_bin_dirs() -> list[str]:
    bins: list[str] = []
    for base in _fnm_base_dirs():
        bins.append(os.path.join(base, "aliases", "default", "bin"))
    return [path for path in bins if os.path.isdir(path)]


async def _detect_fnm_provider() -> NodeProvider | None:
    fnm = _find_fnm_binary()
    if not fnm:
        return None

    node_result = await _run_optional([fnm, "exec", "--using", "default", "which", "node"])
    npm_result = await _run_optional([fnm, "exec", "--using", "default", "which", "npm"])
    version_result = await _run_optional([fnm, "exec", "--using", "default", "node", "--version"])
    if (
        node_result
        and node_result.returncode == 0
        and npm_result
        and npm_result.returncode == 0
        and version_result
        and version_result.returncode == 0
    ):
        node_path = node_result.output.splitlines()[0].strip()
        npm_path = npm_result.output.splitlines()[0].strip()
        bin_paths = [os.path.dirname(node_path), os.path.dirname(npm_path)]
        return NodeProvider(
            name="fnm",
            node_path=node_path,
            npm_path=npm_path,
            bin_paths=list(dict.fromkeys(bin_paths)),
            command_prefix=[fnm, "exec", "--using", "default"],
        )

    for bin_dir in _fnm_default_bin_dirs():
        node_path = os.path.join(bin_dir, "node")
        npm_path = os.path.join(bin_dir, "npm")
        if _is_executable(node_path) and _is_executable(npm_path):
            return NodeProvider(name="fnm", node_path=node_path, npm_path=npm_path, bin_paths=[bin_dir])

    return NodeProvider(name="fnm", node_path=None, npm_path=None, error="node_not_configured")


def _detect_nvm_provider() -> NodeProvider | None:
    candidates = [
        path for path in glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")) if _is_executable(path)
    ]
    if not candidates:
        return None
    candidates.sort(key=_parse_node_version, reverse=True)
    node_path = candidates[0]
    bin_dir = os.path.dirname(node_path)
    npm_path = os.path.join(bin_dir, "npm")
    if not _is_executable(npm_path):
        return None
    return NodeProvider(name="nvm", node_path=node_path, npm_path=npm_path, bin_paths=[bin_dir])


def _detect_system_provider() -> NodeProvider | None:
    node_path = shutil.which("node")
    npm_path = shutil.which("npm")
    if node_path and npm_path:
        return NodeProvider(
            name="system",
            node_path=node_path,
            npm_path=npm_path,
            bin_paths=list(dict.fromkeys([os.path.dirname(node_path), os.path.dirname(npm_path)])),
        )
    for bin_dir in ["/usr/local/bin", "/usr/bin"]:
        node_candidate = os.path.join(bin_dir, "node")
        npm_candidate = os.path.join(bin_dir, "npm")
        if _is_executable(node_candidate) and _is_executable(npm_candidate):
            return NodeProvider(name="system", node_path=node_candidate, npm_path=npm_candidate, bin_paths=[bin_dir])
    return None


async def detect_node_provider() -> NodeProvider | None:
    fnm_provider = await _detect_fnm_provider()
    if fnm_provider is not None:
        return fnm_provider
    nvm_provider = _detect_nvm_provider()
    if nvm_provider is not None:
        return nvm_provider
    return _detect_system_provider()


async def _get_node_version(provider: NodeProvider | None) -> str | None:
    if provider is None or provider.error:
        return None
    result = await _run_optional(provider.command("node", "--version"), env=_provider_env(provider))
    if result is None or result.returncode != 0:
        return None
    return _first_version(result.output)


async def _get_appium_version(provider: NodeProvider | None = None) -> str | None:
    appium = _find_appium()
    env = _provider_env(provider)
    result = await _run_optional([appium, "--version"], env=env)
    if result is None or result.returncode != 0:
        if provider and provider.command_prefix:
            result = await _run_optional(provider.command("appium", "--version"), env=env)
        if result is None or result.returncode != 0:
            return None
    return _first_version(result.output)


async def _get_go_ios_version(provider: NodeProvider | None = None) -> str | None:
    env = _provider_env(provider)
    result = await _run_optional(["ios", "--version"], env=env)
    if result is None or result.returncode != 0:
        if provider and provider.command_prefix:
            result = await _run_optional(provider.command("ios", "--version"), env=env)
        if result is None or result.returncode != 0:
            return None
    return _first_version(result.output) or result.output.splitlines()[0].strip()


def _parse_manifest_version(text: str) -> str | None:
    values: dict[str, str] = {}
    current_key: str | None = None
    for raw_line in text.splitlines():
        if raw_line.startswith(" ") and current_key:
            values[current_key] += raw_line.strip()
            continue
        if ":" not in raw_line:
            current_key = None
            continue
        key, value = raw_line.split(":", 1)
        current_key = key.strip()
        values[current_key] = value.strip()
    return values.get("Selenium-Version") or values.get("Implementation-Version")


def get_selenium_jar_version(jar_path: str) -> str | None:
    if not os.path.isfile(jar_path):
        return None
    try:
        with zipfile.ZipFile(jar_path) as jar, jar.open("META-INF/MANIFEST.MF") as manifest:
            return _parse_manifest_version(manifest.read().decode(errors="replace"))
    except (OSError, KeyError, zipfile.BadZipFile):
        return None


async def get_tool_status() -> dict[str, Any]:
    provider = await detect_node_provider()
    if provider and provider.bin_paths:
        _prepend_process_path(provider.bin_paths)
    node_version = await _get_node_version(provider)
    appium_version = await _get_appium_version(provider)
    go_ios_version = await _get_go_ios_version(provider)
    return {
        "appium": appium_version,
        "node": node_version,
        "node_provider": provider.name if provider and not provider.error else None,
        "node_error": provider.error if provider and provider.error else None,
        "go_ios": go_ios_version,
        "selenium_jar": get_selenium_jar_version(agent_settings.selenium_server_jar),
        "selenium_jar_path": agent_settings.selenium_server_jar,
    }


async def ensure_appium(version: str) -> dict[str, Any]:
    version = version.strip()
    if not version:
        return {"success": True, "action": "skipped"}

    provider = await detect_node_provider()
    if provider and provider.bin_paths:
        _prepend_process_path(provider.bin_paths)

    current_version = await _get_appium_version(provider)
    if current_version == version:
        return {
            "success": True,
            "action": "none",
            "version": current_version,
            "node_provider": provider.name if provider and not provider.error else None,
        }

    if provider is None:
        return {"success": False, "error": "node_not_found"}
    if provider.error:
        return {"success": False, "error": provider.error, "node_provider": provider.name}
    if not provider.npm_path and not provider.command_prefix:
        return {"success": False, "error": "node_not_found", "node_provider": provider.name}

    cmd = provider.command("npm", "install", "-g", f"appium@{version}")
    result = await _run_optional(cmd, timeout=APPIUM_INSTALL_TIMEOUT_SEC, env=_provider_env(provider))
    if result is None:
        return {"success": False, "error": "install_failed", "node_provider": provider.name}
    if result.returncode != 0:
        return {
            "success": False,
            "error": result.output or "npm install failed",
            "node_provider": provider.name,
        }

    installed_version = await _get_appium_version(provider)
    if installed_version != version:
        return {
            "success": False,
            "error": "installed_version_mismatch",
            "expected_version": version,
            "version": installed_version,
            "node_provider": provider.name,
            "output": result.output,
        }

    await refresh_capabilities_snapshot()
    return {
        "success": True,
        "action": "installed" if current_version is None else "updated",
        "version": installed_version,
        "previous_version": current_version,
        "node_provider": provider.name,
        "output": result.output,
    }


async def ensure_selenium_jar(version: str, jar_path: str) -> dict[str, Any]:
    version = version.strip()
    if not version:
        return {"success": True, "action": "skipped"}
    normalized_version = _normalize_selenium_version(version)
    if normalized_version is None:
        return {"success": False, "error": "invalid_selenium_version", "version": get_selenium_jar_version(jar_path)}
    version = normalized_version

    current_version = get_selenium_jar_version(jar_path)
    if current_version == version:
        return {"success": True, "action": "none", "version": current_version, "path": jar_path}

    url = SELENIUM_JAR_URL.format(version=version)
    parent = Path(jar_path).expanduser().resolve().parent
    tmp_path: str | None = None
    try:
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".selenium-server-", suffix=".jar", dir=str(parent))
        os.close(fd)
        async with httpx.AsyncClient(timeout=SELENIUM_DOWNLOAD_TIMEOUT_SEC, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
        with open(tmp_path, "wb") as f:
            f.write(response.content)
        downloaded_version = get_selenium_jar_version(tmp_path)
        if downloaded_version != version:
            return {
                "success": False,
                "error": "downloaded_version_mismatch",
                "expected_version": version,
                "version": downloaded_version,
            }
        os.replace(tmp_path, jar_path)
        tmp_path = None
    except httpx.HTTPError as exc:
        return {"success": False, "error": str(exc), "version": current_version}
    except OSError as exc:
        return {"success": False, "error": str(exc), "version": current_version}
    finally:
        if tmp_path:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

    await refresh_capabilities_snapshot()
    return {
        "success": True,
        "action": "downloaded" if current_version is None else "updated",
        "version": version,
        "previous_version": current_version,
        "path": jar_path,
    }


async def ensure_tools(
    appium_version: str | None,
    selenium_jar_version: str | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if appium_version is not None:
        result["appium"] = await ensure_appium(appium_version)
    if selenium_jar_version is not None:
        result["selenium_jar"] = await ensure_selenium_jar(selenium_jar_version, agent_settings.selenium_server_jar)
    return result
