"""Verify test fakes satisfy their protocols."""

from __future__ import annotations

import pytest

from app.core.protocols import EmitProtocol, SettingsReader
from tests.fakes.emit import FakeEmit
from tests.fakes.settings import FakeSettingsReader


@pytest.mark.asyncio
async def test_fake_emit_captures_events() -> None:
    emit = FakeEmit()
    await emit("device.online", {"device_id": "abc"})
    await emit("device.offline", {"device_id": "def"})
    assert len(emit.emitted) == 2
    assert emit.emitted[0] == ("device.online", {"device_id": "abc"})
    assert emit.emitted[1] == ("device.offline", {"device_id": "def"})


def test_fake_emit_satisfies_protocol() -> None:
    emit = FakeEmit()
    assert isinstance(emit, EmitProtocol)


def test_fake_settings_reader_returns_defaults() -> None:
    reader = FakeSettingsReader()
    assert reader.get("any.key") == ""


def test_fake_settings_reader_returns_overrides() -> None:
    reader = FakeSettingsReader({"timeout": 30, "retries": 3})
    assert reader.get("timeout") == 30
    assert reader.get("retries") == 3


def test_fake_settings_reader_satisfies_protocol() -> None:
    reader = FakeSettingsReader()
    assert isinstance(reader, SettingsReader)
