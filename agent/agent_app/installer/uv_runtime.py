from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_app.installer.identity import OperatorIdentity


@dataclass(frozen=True)
class UvRuntime:
    bin_path: Path | None
    source: str
    searched: tuple[str, ...] = ()


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def discover_uv(*, operator: OperatorIdentity, override: Path | None) -> UvRuntime:
    searched: list[str] = []

    if override is not None:
        searched.append(str(override))
        if _is_executable(override):
            return UvRuntime(bin_path=override, source="explicit", searched=tuple(searched))

    operator_candidate = operator.home / ".local" / "bin" / "uv"
    searched.append(str(operator_candidate))
    if _is_executable(operator_candidate):
        return UvRuntime(bin_path=operator_candidate, source="operator_home", searched=tuple(searched))

    current_home_candidate = Path(os.path.expanduser("~")) / ".local" / "bin" / "uv"
    if current_home_candidate != operator_candidate:
        searched.append(str(current_home_candidate))
        if _is_executable(current_home_candidate):
            return UvRuntime(bin_path=current_home_candidate, source="current_home", searched=tuple(searched))

    which = shutil.which("uv")
    if which:
        searched.append(which)
        return UvRuntime(bin_path=Path(which), source="path", searched=tuple(searched))

    return UvRuntime(bin_path=None, source="missing", searched=tuple(searched))


def build_upgrade_command(
    runtime: UvRuntime,
    *,
    operator: OperatorIdentity,
    package_spec: str,
    os_name: str,
    current_uid: int,
) -> list[str]:
    if runtime.bin_path is None:
        raise RuntimeError(f"uv not found for operator {operator.login!r}; searched: {runtime.searched}")
    bin_path = str(runtime.bin_path)
    if current_uid == operator.uid:
        return [bin_path, "tool", "upgrade", package_spec]
    home_arg = f"HOME={operator.home}"
    if os_name == "Linux":
        runuser = shutil.which("runuser")
        if runuser:
            return [runuser, "-u", operator.login, "--", "env", home_arg, bin_path, "tool", "upgrade", package_spec]
        return ["sudo", "-u", operator.login, "env", home_arg, bin_path, "tool", "upgrade", package_spec]
    if os_name == "Darwin":
        return ["sudo", "-u", operator.login, "env", home_arg, bin_path, "tool", "upgrade", package_spec]
    raise RuntimeError(f"unsupported OS for uv upgrade: {os_name!r}")
