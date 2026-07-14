from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.core.leader import state_store


async def test_transactional_presence_snapshot_merges_on_normal_exit() -> None:
    parent = state_store.PresenceSnapshot(
        namespaces=frozenset({"fold"}),
        present={("fold", "existing")},
    )
    db = AsyncMock()

    async with state_store.transactional_presence_snapshot(parent):
        await state_store.delete_value(db, "fold", "existing")
        await state_store.set_value(db, "fold", "new", True)
        assert parent.present == {("fold", "existing")}

    assert parent.present == {("fold", "new")}
    assert db.execute.await_count == 2


async def test_transactional_presence_snapshot_discards_on_exception() -> None:
    parent = state_store.PresenceSnapshot(
        namespaces=frozenset({"fold"}),
        present={("fold", "existing")},
    )
    db = AsyncMock()

    with pytest.raises(RuntimeError, match="rollback"):
        async with state_store.transactional_presence_snapshot(parent):
            await state_store.delete_value(db, "fold", "existing")
            await state_store.set_value(db, "fold", "new", True)
            raise RuntimeError("rollback")

    assert parent.present == {("fold", "existing")}
    assert db.execute.await_count == 2


async def test_transactional_presence_snapshot_restores_outer_context() -> None:
    outer = state_store.PresenceSnapshot(
        namespaces=frozenset({"fold"}),
        present={("fold", "outer")},
    )
    child_seen: state_store.PresenceSnapshot | None = None

    with state_store.presence_snapshot(outer):
        async with state_store.transactional_presence_snapshot(outer):
            child_seen = state_store._presence.get()
            assert child_seen is not outer
        assert state_store._presence.get() is outer

    assert child_seen is not None
    assert state_store._presence.get() is None
