"""Unit tests for the pure ``evaluate_runnable`` blocked-reason evaluator.

``evaluate_runnable`` is the no-DB equivalent of ``assert_runnable``: given an
already-loaded pack it must return the same error ``code`` that ``assert_runnable``
would raise (or ``None`` when runnable). These tests pin that mapping so the batch
serialization path can't drift from the per-device path.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.packs.models import PackState
from app.packs.services.platform_resolver import evaluate_runnable


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
