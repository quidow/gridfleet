"""Shared test fakes implementing core Protocols."""

from tests.fakes.grid import make_fake_grid
from tests.fakes.review import build_diagnostics_export, build_review_service
from tests.fakes.settings import FakeSettingsReader

__all__ = ["FakeSettingsReader", "build_diagnostics_export", "build_review_service", "make_fake_grid"]
