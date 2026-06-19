from __future__ import annotations

import signal
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

import httpx2 as httpx

from gridfleet_testkit.run_lifecycle import HeartbeatThread, register_run_cleanup

if TYPE_CHECKING:
    from gridfleet_testkit.types import JsonObject

CleanupCallback = Callable[[], None]
SignalCallback = Callable[[int, object], None]


class DummyResponse:
    def __init__(self, payload: object, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("request failed", request=httpx.Request("GET", "http://test"), response=None)


def test_register_run_cleanup_default_does_not_install_signals_or_complete_run(monkeypatch):
    registered: list[CleanupCallback] = []
    signal_handlers: list[tuple[signal.Signals, object]] = []

    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.atexit.register", lambda fn: registered.append(fn))
    monkeypatch.setattr(
        "gridfleet_testkit.run_lifecycle.signal.signal", lambda sig, fn: signal_handlers.append((sig, fn))
    )

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def complete_run(self, run_id: str) -> JsonObject:
            self.calls.append(f"complete:{run_id}")
            return {"state": "completed"}

        def cancel_run(self, run_id: str) -> JsonObject:
            self.calls.append(f"cancel:{run_id}")
            return {"state": "cancelled"}

    client = FakeClient()
    cleanup = register_run_cleanup(client, "run-3")

    assert cleanup is registered[0]
    assert signal_handlers == []

    cleanup()

    assert client.calls == []


def test_register_run_cleanup_can_complete_or_cancel_on_exit(monkeypatch):
    registered: list[CleanupCallback] = []
    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.atexit.register", lambda fn: registered.append(fn))

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def complete_run(self, run_id: str) -> JsonObject:
            self.calls.append(f"complete:{run_id}")
            return {"state": "completed"}

        def cancel_run(self, run_id: str) -> JsonObject:
            self.calls.append(f"cancel:{run_id}")
            return {"state": "cancelled"}

    complete_client = FakeClient()
    register_run_cleanup(complete_client, "run-1", on_exit="complete")
    registered[-1]()
    assert complete_client.calls == ["complete:run-1"]

    cancel_client = FakeClient()
    register_run_cleanup(cancel_client, "run-2", on_exit="cancel")
    registered[-1]()
    assert cancel_client.calls == ["cancel:run-2"]


def test_register_run_cleanup_stops_and_joins_heartbeat(monkeypatch):
    registered: list[CleanupCallback] = []
    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.atexit.register", lambda fn: registered.append(fn))

    class FakeClient:
        pass

    class FakeThread:
        def __init__(self) -> None:
            self.stopped = False
            self.joined_with: float | None = None

        def stop(self) -> None:
            self.stopped = True

        def join(self, timeout: float | None = None) -> None:
            self.joined_with = timeout

        def is_alive(self) -> bool:
            return False

    thread = FakeThread()
    register_run_cleanup(FakeClient(), "run-1", heartbeat_thread=thread, join_timeout_sec=2.5)
    registered[0]()

    assert thread.stopped is True
    assert thread.joined_with == 2.5


def test_register_run_cleanup_installs_signal_handlers_only_when_requested(monkeypatch):
    registered: list[CleanupCallback] = []
    installed: dict[signal.Signals, SignalCallback] = {}
    previous_calls: list[tuple[signal.Signals, object]] = []

    def previous_handler(sig: signal.Signals, frame: object) -> None:
        previous_calls.append((sig, frame))

    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.atexit.register", lambda fn: registered.append(fn))
    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.signal.getsignal", lambda _sig: previous_handler)
    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.signal.signal", lambda sig, fn: installed.__setitem__(sig, fn))

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def complete_run(self, run_id: str) -> JsonObject:
            self.calls.append(f"complete:{run_id}")
            return {"state": "completed"}

        def cancel_run(self, run_id: str) -> JsonObject:
            self.calls.append(f"cancel:{run_id}")
            return {"state": "cancelled"}

    client = FakeClient()
    register_run_cleanup(client, "run-9", install_signal_handlers=True)
    installed[signal.SIGTERM](signal.SIGTERM, object())

    assert client.calls == ["cancel:run-9"]
    assert previous_calls[0][0] == signal.SIGTERM


def test_register_run_cleanup_can_skip_signal_chaining(monkeypatch):
    registered: list[CleanupCallback] = []
    installed: dict[signal.Signals, SignalCallback] = {}
    previous_calls: list[signal.Signals] = []

    def previous_handler(sig: signal.Signals, frame: object) -> None:
        previous_calls.append(sig)

    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.atexit.register", lambda fn: registered.append(fn))
    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.signal.getsignal", lambda _sig: previous_handler)
    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.signal.signal", lambda sig, fn: installed.__setitem__(sig, fn))

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def cancel_run(self, run_id: str) -> JsonObject:
            self.calls.append(f"cancel:{run_id}")
            return {"state": "cancelled"}

    client = FakeClient()
    register_run_cleanup(client, "run-10", install_signal_handlers=True, chain_signals=False)
    installed[signal.SIGINT](signal.SIGINT, object())

    assert client.calls == ["cancel:run-10"]
    assert previous_calls == []


def test_register_run_cleanup_warns_when_heartbeat_does_not_join(monkeypatch, caplog):
    registered: list[CleanupCallback] = []
    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.atexit.register", lambda fn: registered.append(fn))

    class FakeClient:
        pass

    class StuckThread:
        def stop(self) -> None:
            return None

        def join(self, timeout: float | None = None) -> None:
            return None

        def is_alive(self) -> bool:
            return True

    register_run_cleanup(FakeClient(), "run-stuck", heartbeat_thread=StuckThread(), join_timeout_sec=0.1)
    registered[0]()

    assert "Heartbeat thread for run run-stuck did not stop" in caplog.text


def test_register_run_cleanup_is_idempotent(monkeypatch):
    registered: list[CleanupCallback] = []
    installed: dict[signal.Signals, SignalCallback] = {}
    raises: list[int] = []

    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.atexit.register", lambda fn: registered.append(fn))
    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.signal.getsignal", lambda _sig: signal.SIG_DFL)
    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.signal.signal", lambda sig, fn: installed.__setitem__(sig, fn))
    # Patch raise_signal so that the SIG_DFL chain path does not actually kill the test process.
    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.signal.raise_signal", lambda sig: raises.append(sig))

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def cancel_run(self, run_id: str) -> JsonObject:
            self.calls.append(f"cancel:{run_id}")
            return {"state": "cancelled"}

        def complete_run(self, run_id: str) -> JsonObject:
            self.calls.append(f"complete:{run_id}")
            return {"state": "completed"}

    client = FakeClient()
    register_run_cleanup(
        client,
        "run-idem",
        install_signal_handlers=True,
        on_exit="complete",
        on_signal="cancel",
    )

    installed[signal.SIGTERM](int(signal.SIGTERM), None)
    registered[0]()

    assert client.calls == ["cancel:run-idem"]


def test_heartbeat_thread_passes_auth(monkeypatch):
    captured: dict[str, object] = {}

    explicit = httpx.BasicAuth("hb-user", "hb-pass")
    thread = HeartbeatThread("http://manager/api", "run-x", interval=0, auth=explicit)

    def fake_post(
        url: str,
        *,
        timeout: int,
        auth: object = None,
    ) -> DummyResponse:
        captured["url"] = url
        captured["auth"] = auth
        thread._stop_event.set()
        return DummyResponse({"state": "active"})

    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.httpx.post", fake_post)

    thread.run()

    assert captured["url"] == "http://manager/api/runs/run-x/heartbeat"
    assert captured["auth"] is explicit


# --- Signal chain semantics: SIG_DFL and SIG_IGN ---


def test_register_run_cleanup_chains_sig_dfl_by_re_raising(monkeypatch):
    """When the previous handler is SIG_DFL, chain_signals=True should restore the default
    and re-raise the signal so the kernel's default action (e.g. terminate on SIGTERM) fires."""
    registered: list[CleanupCallback] = []
    installed: dict[signal.Signals, SignalCallback] = {}
    raises: list[int] = []
    re_set: list[tuple[int, object]] = []

    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.atexit.register", lambda fn: registered.append(fn))
    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.signal.getsignal", lambda _sig: signal.SIG_DFL)

    def fake_signal(sig: signal.Signals, fn: object) -> object:
        installed[sig] = cast("SignalCallback", fn)
        re_set.append((int(sig), fn))
        return None

    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.signal.signal", fake_signal)
    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.signal.raise_signal", lambda sig: raises.append(sig))

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def cancel_run(self, run_id: str) -> JsonObject:
            self.calls.append(f"cancel:{run_id}")
            return {"state": "cancelled"}

    register_run_cleanup(FakeClient(), "run-dfl", install_signal_handlers=True)
    installed[signal.SIGTERM](int(signal.SIGTERM), None)

    assert raises == [int(signal.SIGTERM)]
    # The default handler must be restored *before* re-raising.
    restored = [item for item in re_set if item[1] is signal.SIG_DFL and item[0] == signal.SIGTERM]
    assert restored, "SIG_DFL handler not restored before raise_signal"


def test_register_run_cleanup_chains_sig_ign_as_drop(monkeypatch):
    """When the previous handler is SIG_IGN, chain_signals=True should silently drop the signal
    without re-raising and without invoking raise_signal."""
    registered: list[CleanupCallback] = []
    installed: dict[signal.Signals, SignalCallback] = {}
    raises: list[int] = []

    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.atexit.register", lambda fn: registered.append(fn))
    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.signal.getsignal", lambda _sig: signal.SIG_IGN)
    monkeypatch.setattr(
        "gridfleet_testkit.run_lifecycle.signal.signal",
        lambda sig, fn: installed.__setitem__(sig, fn),
    )
    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.signal.raise_signal", lambda sig: raises.append(sig))

    class FakeClient:
        def cancel_run(self, run_id: str) -> JsonObject:
            return {"state": "cancelled"}

    register_run_cleanup(FakeClient(), "run-ign", install_signal_handlers=True)
    installed[signal.SIGTERM](int(signal.SIGTERM), None)

    assert raises == []


# --- Thread-safety: idempotency under explicit double call ---


def test_register_run_cleanup_idempotent_under_explicit_double_call(monkeypatch):
    """Calling the returned cleanup callable twice must invoke the policy exactly once,
    even without a signal or atexit path (regression for unsynchronized `called` flag)."""
    registered: list[CleanupCallback] = []
    monkeypatch.setattr("gridfleet_testkit.run_lifecycle.atexit.register", lambda fn: registered.append(fn))

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def complete_run(self, run_id: str) -> JsonObject:
            self.calls.append(f"complete:{run_id}")
            return {"state": "completed"}

    client = FakeClient()
    cleanup_fn = register_run_cleanup(client, "run-double", on_exit="complete")
    cleanup_fn()
    cleanup_fn()  # second invocation must be a no-op

    assert client.calls == ["complete:run-double"]
