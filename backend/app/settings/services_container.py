"""Settings domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.settings.service import SettingsService
    from app.settings.service_config import SettingsConfigService


@dataclass(frozen=True, slots=True)
class SettingsServices:
    service: SettingsService
    config: SettingsConfigService
