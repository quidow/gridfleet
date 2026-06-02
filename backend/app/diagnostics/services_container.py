"""Diagnostics domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.diagnostics.protocols import DiagnosticExportProtocol


@dataclass(frozen=True, slots=True)
class DiagnosticsServices:
    export: DiagnosticExportProtocol
