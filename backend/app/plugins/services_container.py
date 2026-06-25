"""Plugins domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.plugins.service import PluginService


@dataclass(frozen=True, slots=True)
class PluginServices:
    plugin: PluginService
