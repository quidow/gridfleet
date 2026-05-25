"""Verify that Protocol definitions are structurally sound."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.protocols import EmitProtocol, SettingsReader, SettingsWriter


class _FakeEmit:
    async def __call__(self, event_type: str, payload: dict[str, Any]) -> None:
        pass


class _FakeSettingsReader:
    def get(self, key: str) -> str:
        return ""

    def get_int(self, key: str) -> int:
        return 0


class _FakeSettingsWriter:
    async def set(self, db: object, key: str, value: str) -> None:
        pass


def test_fake_emit_satisfies_protocol() -> None:
    emit: EmitProtocol = _FakeEmit()
    assert callable(emit)


def test_fake_settings_reader_satisfies_protocol() -> None:
    reader: SettingsReader = _FakeSettingsReader()
    assert reader.get("x") == ""
    assert reader.get_int("x") == 0


def test_fake_settings_writer_satisfies_protocol() -> None:
    writer: SettingsWriter = _FakeSettingsWriter()
    assert writer is not None
