"""Shared test fakes implementing core Protocols."""

from tests.fakes.session_factory import FakeSessionFactory, RecordingSessionFactory
from tests.fakes.settings import FakeSettingsReader

__all__ = ["FakeSessionFactory", "FakeSettingsReader", "RecordingSessionFactory"]
