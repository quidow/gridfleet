import asyncio
import sys
from pathlib import Path

import pytest

from agent_app.pack import adapter_loader

pytestmark = pytest.mark.asyncio


def _make_install_dir(root: Path, label: str) -> Path:
    install_dir = (root / label / "site").resolve()
    (install_dir / "adapter").mkdir(parents=True, exist_ok=True)
    (install_dir / "adapter" / "__init__.py").write_text(
        f"PACK_LABEL = {label!r}\n",
        encoding="utf-8",
    )
    return install_dir


def _adapter_module_names() -> list[str]:
    return [name for name in sys.modules if name == "adapter" or name.startswith("adapter.")]


async def test_cancelled_adapter_call_does_not_leak_import_state(tmp_path: Path) -> None:
    """If a wrapped adapter call is cancelled mid-await, it must unwind the
    process-global import state that `_activate_adapter_site()` changed."""

    adapter_loader._adapter_cache_clear()
    pack_a_dir = _make_install_dir(tmp_path, "pack_a")
    original_path = list(sys.path)
    original_modules = {name: sys.modules[name] for name in _adapter_module_names()}

    try:
        slow_started = asyncio.Event()

        class _SlowInstance:
            def slow(self) -> "asyncio.Future[None]":
                import adapter  # type: ignore[import-not-found]

                assert adapter.PACK_LABEL == "pack_a"
                slow_started.set()
                return asyncio.get_running_loop().create_future()

        wrapped_a = adapter_loader._IsolatedAdapter(
            _SlowInstance(),
            pack_a_dir,
            pack_id="pack-a",
            release="0.0.1",
        )

        async def call_a() -> None:
            # `_IsolatedAdapter.__getattr__` returns a coroutine fn; calling it
            # returns the coroutine that we await on.
            await wrapped_a.slow()

        task = asyncio.create_task(call_a())
        await asyncio.wait_for(slow_started.wait(), timeout=1)
        assert sys.path[0] == str(pack_a_dir)
        assert task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert str(pack_a_dir) not in sys.path, (
            f"cancelled adapter call leaked its install dir into sys.path: {sys.path!r}"
        )
        assert _adapter_module_names() == [], (
            f"cancelled adapter call leaked adapter modules in sys.modules: {_adapter_module_names()!r}"
        )
    finally:
        sys.path[:] = original_path
        for name in _adapter_module_names():
            sys.modules.pop(name, None)
        sys.modules.update(original_modules)
