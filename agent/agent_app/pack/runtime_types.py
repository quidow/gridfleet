from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AppiumInstallable:
    source: str
    package: str
    version: str
    recommended: str | None
    known_bad: list[str]
    github_repo: str | None = None
