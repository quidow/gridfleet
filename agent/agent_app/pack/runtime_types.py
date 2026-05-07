from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class AppiumInstallable:
    source: str
    package: str
    version: str
    recommended: str | None
    known_bad: list[str]
    github_repo: str | None = None
    available_versions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RuntimePolicy:
    strategy: Literal["recommended", "latest_patch", "exact"] = "recommended"
    appium_server_version: str | None = None
    appium_driver_version: str | None = None
