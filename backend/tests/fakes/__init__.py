"""Shared test fakes implementing core Protocols."""

from tests.fakes.review import build_review_service
from tests.fakes.settings import FakeSettingsReader

__all__ = ["FakeSettingsReader", "build_review_service"]
