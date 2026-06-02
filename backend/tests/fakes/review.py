"""Helper constructing a real ReviewService for tests.

``ReviewService`` is a thin wrapper around the diagnostic-snapshot capture
edge; tests that only need a constructed dependency use this factory rather
than wiring the diagnostics service by hand.
"""

from __future__ import annotations

from app.devices.services.diagnostics_export import DiagnosticExportService
from app.devices.services.review import ReviewService


def build_review_service() -> ReviewService:
    return ReviewService(diagnostics=DiagnosticExportService())


def build_diagnostics_export() -> DiagnosticExportService:
    return DiagnosticExportService()
