"""Unit tests for the pure ``evaluate_runnable`` blocked-reason evaluator.

``evaluate_runnable`` is the no-DB equivalent of ``assert_runnable``: given an
already-loaded pack it must return the same error ``code`` that ``assert_runnable``
would raise (or ``None`` when runnable). These tests pin that mapping so the batch
serialization path can't drift from the per-device path.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from app.core.errors import PackDisabledError, PackDrainingError, PackUnavailableError, PlatformRemovedError
from app.devices.services.readiness import load_packs_by_ids
from app.packs.models import DriverPack, PackState
from app.packs.services.platform_resolver import assert_runnable, evaluate_runnable
from tests.pack.factories import seed_test_packs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _pack(state: PackState, *, platform_ids: tuple[str, ...] = ()) -> SimpleNamespace:
    release = SimpleNamespace(
        release="1.0.0",
        platforms=[SimpleNamespace(manifest_platform_id=pid) for pid in platform_ids],
    )
    return SimpleNamespace(state=state, releases=[release], current_release="1.0.0")


def test_none_pack_is_unavailable() -> None:
    assert evaluate_runnable(None, platform_id="android_mobile") == "pack_unavailable"


def test_disabled_pack() -> None:
    assert evaluate_runnable(_pack(PackState.disabled), platform_id="android_mobile") == "pack_disabled"


def test_draining_pack() -> None:
    assert evaluate_runnable(_pack(PackState.draining), platform_id="android_mobile") == "pack_draining"


def test_platform_missing_from_release() -> None:
    pack = _pack(PackState.enabled, platform_ids=("ios",))
    assert evaluate_runnable(pack, platform_id="android_mobile") == "platform_removed"


def test_runnable_returns_none() -> None:
    pack = _pack(PackState.enabled, platform_ids=("android_mobile",))
    assert evaluate_runnable(pack, platform_id="android_mobile") is None


@pytest.mark.db
@pytest.mark.parametrize("state", list(PackState))
async def test_evaluate_runnable_agrees_with_assert_runnable_across_all_states(
    db_session: AsyncSession, state: PackState
) -> None:
    """Drift guard: for EVERY PackState, evaluate_runnable's code must equal the code
    assert_runnable raises (or both None). Adding a new PackState without updating both
    fails here — the batch device-list path cannot silently diverge from the per-device
    path and the allocator."""
    pack_id, platform_id = "appium-uiautomator2", "android_mobile"
    await seed_test_packs(db_session)
    pack_row = await db_session.get(DriverPack, pack_id)
    assert pack_row is not None
    pack_row.state = state
    await db_session.commit()

    catalog = await load_packs_by_ids(db_session, {pack_id})
    eval_code = evaluate_runnable(catalog.get(pack_id), platform_id=platform_id)

    assert_code: str | None = None
    try:
        await assert_runnable(db_session, pack_id=pack_id, platform_id=platform_id)
    except (PackUnavailableError, PackDisabledError, PackDrainingError, PlatformRemovedError) as exc:
        assert_code = exc.code

    assert eval_code == assert_code, f"divergence for PackState.{state}: evaluate={eval_code!r} assert={assert_code!r}"
