"""Portability domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.portability.services.export import PortabilityExportService
    from app.portability.services.import_bundle import PortabilityImportService


@dataclass(frozen=True, slots=True)
class PortabilityServices:
    export: PortabilityExportService
    import_: PortabilityImportService
