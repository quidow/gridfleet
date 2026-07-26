"""Shared test fakes implementing core Protocols."""

from tests.fakes.review import build_review_service
from tests.fakes.session_factory import FakeSessionFactory
from tests.fakes.settings import FakeSettingsReader

__all__ = ["FakeSessionFactory", "FakeSettingsReader", "build_review_service"]
