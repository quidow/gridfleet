from __future__ import annotations

import gc

from app.core import gc_tuning


def test_tune_after_startup_sets_thresholds_and_freezes() -> None:
    saved = gc.get_threshold()
    try:
        gc_tuning.tune_after_startup()
        assert gc.get_threshold() == (gc_tuning.GEN0_THRESHOLD, saved[1], saved[2])
        assert gc.get_freeze_count() > 0
    finally:
        gc.set_threshold(*saved)
        gc.unfreeze()
