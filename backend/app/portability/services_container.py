"""Portability domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.portability.protocols import (
        InventoryExportProtocol,
        PortabilityExportProtocol,
        PortabilityImportProtocol,
    )


@dataclass(frozen=True, slots=True)
class PortabilityServices:
    export: PortabilityExportProtocol
    import_: PortabilityImportProtocol
    inventory: InventoryExportProtocol
