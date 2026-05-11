"""Public GridFleet client helpers for external test suites."""

from __future__ import annotations

import atexit
import logging
import os
import signal
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import FrameType

import httpx

DEFAULT_GRID_URL = "http://localhost:4444"
DEFAULT_GRIDFLEET_API_URL = "http://localhost:8000/api"

logger = logging.getLogger("gridfleet_testkit")


def _default_grid_url() -> str:
    return os.getenv("GRID_URL", DEFAULT_GRID_URL)


def _default_api_url() -> str:
    return os.getenv("GRIDFLEET_API_URL", DEFAULT_GRIDFLEET_API_URL)


def __getattr__(name: str) -> str:
    if name == "GRID_URL":
        return _default_grid_url()
    if name == "GRIDFLEET_API_URL":
        return _default_api_url()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _default_auth() -> httpx.BasicAuth | None:
    """Build httpx Basic auth from env vars, or return None when unset."""
    username = os.getenv("GRIDFLEET_TESTKIT_USERNAME")
    password = os.getenv("GRIDFLEET_TESTKIT_PASSWORD")
    if not username or not password:
        return None
    return httpx.BasicAuth(username, password)


class UnknownIncludeError(ValueError):
    """Backend rejected one or more `?include=` keys."""

    def __init__(self, values: list[str]) -> None:
        super().__init__(f"Backend rejected unknown include values: {values}")
        self.values = values


class ReserveCapabilitiesUnsupportedError(ValueError):
    """`?include=capabilities` is not supported on reserve."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or "include=capabilities is not supported on reserve")


def _raise_for_status(resp: Any, *, run_id: str) -> None:
    del run_id
    if resp.status_code == 422:
        try:
            payload = resp.json()
        except Exception:
            payload = None
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            details = error.get("details")
            if isinstance(details, dict):
                code = details.get("code")
                if code == "unknown_include":
                    values = details.get("values")
                    raise UnknownIncludeError(values if isinstance(values, list) else [])
                if code == "reserve_capabilities_unsupported":
                    raise ReserveCapabilitiesUnsupportedError(str(error.get("message") or ""))
    resp.raise_for_status()


def _query_params(values: dict[str, Any]) -> list[tuple[str, str | int | float | bool | None]]:
    params: list[tuple[str, str | int | float | bool | None]] = []
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            params.append((key, str(value).lower()))
        else:
            params.append((key, str(value)))
    return params


def _normalize_include(include: Sequence[str] | None) -> tuple[str, ...] | None:
    if include is None:
        return None
    if isinstance(include, (str, bytes)):
        raise TypeError(
            "include must be a sequence of strings, not a string itself "
            "(e.g. include=('config',), not include='config')"
        )
    return tuple(include)


def _include_param(include: tuple[str, ...] | None) -> list[tuple[str, str | int | float | bool | None]] | None:
    if include is None:
        return None
    values = [v for v in include if v]
    if not values:
        return None
    return [("include", ",".join(values))]


def _raise_or_warn(operation: str, suppress_errors: bool, exc: Exception) -> None:
    if not suppress_errors:
        raise exc
    logger.warning("Failed to %s with GridFleet: %s", operation, exc)


class HeartbeatThread(threading.Thread):
    """Background thread that sends periodic heartbeat pings for an active test run."""

    def __init__(
        self,
        base_url: str,
        run_id: str,
        interval: int = 30,
        auth: httpx.BasicAuth | None = None,
    ):
        super().__init__(daemon=True)
        self.base_url = base_url.rstrip("/")
        self.run_id = run_id
        self.interval = interval
        self._auth = auth
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            try:
                resp = httpx.post(
                    f"{self.base_url}/runs/{self.run_id}/heartbeat",
                    timeout=10,
                    auth=self._auth,
                )
                resp.raise_for_status()
                result = resp.json()
                if result.get("state") in ("expired", "cancelled"):
                    logger.warning("Run %s is %s, stopping heartbeat", self.run_id, result["state"])
                    break
            except Exception:
                logger.debug("Heartbeat failed for run %s, will retry", self.run_id)

    def stop(self) -> None:
        self._stop_event.set()


class GridFleetClient:
    """Client for the GridFleet API, used by test fixtures and CI flows."""

    def __init__(
        self,
        base_url: str | None = None,
        auth: httpx.BasicAuth | None = None,
    ):
        self.base_url = (base_url or _default_api_url()).rstrip("/")
        self._auth = auth if auth is not None else _default_auth()

    def list_devices(
        self,
        *,
        pack_id: str | None = None,
        platform_id: str | None = None,
        status: str | None = None,
        host_id: str | None = None,
        identity_value: str | None = None,
        connection_target: str | None = None,
        device_type: str | None = None,
        connection_type: str | None = None,
        os_version: str | None = None,
        search: str | None = None,
        hardware_health_status: str | None = None,
        hardware_telemetry_state: str | None = None,
        needs_attention: bool | None = None,
        tags: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """List devices with backend filter passthrough."""
        params = _query_params(
            {
                "pack_id": pack_id,
                "platform_id": platform_id,
                "status": status,
                "host_id": host_id,
                "identity_value": identity_value,
                "connection_target": connection_target,
                "device_type": device_type,
                "connection_type": connection_type,
                "os_version": os_version,
                "search": search,
                "hardware_health_status": hardware_health_status,
                "hardware_telemetry_state": hardware_telemetry_state,
                "needs_attention": needs_attention,
            }
        )
        if tags:
            params.extend((f"tags.{key}", value) for key, value in tags.items())
        resp = httpx.get(
            f"{self.base_url}/devices",
            params=params,
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return cast("list[dict[str, Any]]", payload["items"])
        return cast("list[dict[str, Any]]", payload)

    def get_device(self, device_id: str) -> dict[str, Any]:
        """Fetch one device detail row by backend device id."""
        resp = httpx.get(
            f"{self.base_url}/devices/{device_id}",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def get_device_by_connection_target(self, target: str) -> dict[str, Any]:
        """Fetch one device detail row by runtime connection target."""
        resp = httpx.get(
            f"{self.base_url}/devices/by-connection-target/{quote(target, safe='')}",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def resolve_device_id_by_connection_target(self, connection_target: str) -> str:
        """Look up the backend device id for a runtime connection target."""
        device = self.get_device_by_connection_target(connection_target)
        return cast("str", device["id"])

    def get_device_config(self, connection_target: str) -> dict[str, Any]:
        """Fetch device config by looking up the current runtime connection target."""
        device_id = self.resolve_device_id_by_connection_target(connection_target)
        config_resp = httpx.get(
            f"{self.base_url}/devices/{device_id}/config",
            timeout=10,
            auth=self._auth,
        )
        config_resp.raise_for_status()
        return cast("dict[str, Any]", config_resp.json())

    def get_device_capabilities(self, device_id: str) -> dict[str, Any]:
        """Fetch the current Appium capabilities for a specific device."""
        resp = httpx.get(
            f"{self.base_url}/devices/{device_id}/capabilities",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def get_device_test_data(self, device_id: str) -> dict[str, Any]:
        """Fetch operator-attached free-form test data for a specific device."""
        resp = httpx.get(
            f"{self.base_url}/devices/{device_id}/test_data",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def replace_device_test_data(self, device_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Replace device test_data with the supplied object."""
        resp = httpx.put(
            f"{self.base_url}/devices/{device_id}/test_data",
            json=body,
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def merge_device_test_data(self, device_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Deep-merge into device test_data."""
        resp = httpx.patch(
            f"{self.base_url}/devices/{device_id}/test_data",
            json=body,
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def get_driver_pack_catalog(self) -> dict[str, Any]:
        """Fetch enabled driver pack catalog data used for Appium platform selection."""
        resp = httpx.get(
            f"{self.base_url}/driver-packs/catalog",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def reserve_devices(
        self,
        name: str,
        requirements: list[dict[str, Any]],
        ttl_minutes: int = 60,
        heartbeat_timeout_sec: int = 120,
        created_by: str | None = None,
        *,
        include: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Reserve devices for a test run and return the manager response."""
        include_tuple = _normalize_include(include)
        if include_tuple is not None and "capabilities" in include_tuple:
            raise ReserveCapabilitiesUnsupportedError("include='capabilities' is not supported on reserve")
        resp = httpx.post(
            f"{self.base_url}/runs",
            json={
                "name": name,
                "requirements": requirements,
                "ttl_minutes": ttl_minutes,
                "heartbeat_timeout_sec": heartbeat_timeout_sec,
                "created_by": created_by,
            },
            params=_include_param(include_tuple),
            timeout=30,
            auth=self._auth,
        )
        _raise_for_status(resp, run_id="")
        return cast("dict[str, Any]", resp.json())

    def signal_ready(self, run_id: str) -> dict[str, Any]:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/ready",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def signal_active(self, run_id: str) -> dict[str, Any]:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/active",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def heartbeat(self, run_id: str) -> dict[str, Any]:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/heartbeat",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def report_preparation_failure(
        self,
        run_id: str,
        device_id: str,
        message: str,
        source: str = "ci_preparation",
        *,
        suppress_errors: bool = False,
    ) -> dict[str, Any] | None:
        try:
            resp = httpx.post(
                f"{self.base_url}/runs/{run_id}/devices/{device_id}/preparation-failed",
                json={"message": message, "source": source},
                timeout=10,
                auth=self._auth,
            )
            resp.raise_for_status()
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            _raise_or_warn("report preparation failure", suppress_errors, exc)
            return None
        return cast("dict[str, Any]", resp.json())

    def register_session(
        self,
        *,
        session_id: str,
        test_name: str | None = None,
        device_id: str | None = None,
        connection_target: str | None = None,
        status: str = "running",
        requested_pack_id: str | None = None,
        requested_platform_id: str | None = None,
        requested_device_type: str | None = None,
        requested_connection_type: str | None = None,
        requested_capabilities: dict[str, Any] | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        run_id: str | None = None,
        suppress_errors: bool = True,
    ) -> dict[str, Any] | None:
        """Register a Grid/Appium session with the manager."""
        try:
            resp = httpx.post(
                f"{self.base_url}/sessions",
                json={
                    "session_id": session_id,
                    "test_name": test_name,
                    "device_id": device_id,
                    "connection_target": connection_target,
                    "status": status,
                    "requested_pack_id": requested_pack_id,
                    "requested_platform_id": requested_platform_id,
                    "requested_device_type": requested_device_type,
                    "requested_connection_type": requested_connection_type,
                    "requested_capabilities": requested_capabilities,
                    "error_type": error_type,
                    "error_message": error_message,
                    "run_id": run_id,
                },
                timeout=5,
                auth=self._auth,
            )
            resp.raise_for_status()
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            _raise_or_warn("register session", suppress_errors, exc)
            return None
        return cast("dict[str, Any]", resp.json())

    def notify_session_finished(
        self,
        session_id: str,
        *,
        suppress_errors: bool = True,
    ) -> None:
        """Tell the manager the WebDriver session has ended.

        Idempotent on the backend — repeated calls are a no-op once the
        Session row is marked ended.
        """
        try:
            resp = httpx.post(
                f"{self.base_url}/sessions/{session_id}/finished",
                timeout=5,
                auth=self._auth,
            )
            resp.raise_for_status()
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            _raise_or_warn("notify session finished", suppress_errors, exc)

    def update_session_status(
        self,
        session_id: str,
        status: str,
        *,
        suppress_errors: bool = True,
    ) -> dict[str, Any] | None:
        """Update a registered session status."""
        try:
            resp = httpx.patch(
                f"{self.base_url}/sessions/{session_id}/status",
                json={"status": status},
                timeout=5,
                auth=self._auth,
            )
            resp.raise_for_status()
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            _raise_or_warn("report session status", suppress_errors, exc)
            return None
        return cast("dict[str, Any]", resp.json())

    def register_session_from_driver(
        self,
        driver: Any,
        *,
        test_name: str | None = None,
        run_id: str | None = None,
        suppress_errors: bool = True,
    ) -> dict[str, Any] | None:
        """Extract session metadata from an Appium driver and register it.

        Also wraps ``driver.quit`` so that the first call after a successful
        registration fires :meth:`notify_session_finished` automatically.
        Errors from notify are suppressed — they must never break the caller.
        """
        capabilities = getattr(driver, "capabilities", {})
        if not isinstance(capabilities, dict):
            capabilities = {}
        session_id = getattr(driver, "session_id", None)
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("Created Appium driver did not expose a session ID")
        device_id = capabilities.get("appium:gridfleet:deviceId") or capabilities.get("gridfleet:deviceId")
        connection_target = capabilities.get("appium:udid") or capabilities.get("appium:deviceName")
        result = self.register_session(
            session_id=session_id,
            test_name=test_name,
            device_id=device_id if isinstance(device_id, str) and device_id else None,
            connection_target=connection_target if isinstance(connection_target, str) and connection_target else None,
            requested_capabilities=capabilities,
            run_id=run_id,
            suppress_errors=suppress_errors,
        )
        self._wrap_quit_for_notify(driver, session_id)
        return result

    def _wrap_quit_for_notify(self, driver: Any, session_id: str) -> None:
        """Replace ``driver.quit`` with a wrapper that also notifies the manager.

        The notify fires at most once per registration: after the first quit
        succeeds, subsequent quit() calls run the underlying quit but do
        NOT post to /finished again. The underlying quit still runs every
        call.

        Raises AttributeError if the driver lacks a quit method.
        """
        original_quit = driver.quit
        notified: dict[str, bool] = {"done": False}

        def wrapped_quit(*args: Any, **kwargs: Any) -> Any:
            try:
                return original_quit(*args, **kwargs)
            finally:
                if not notified["done"]:
                    notified["done"] = True
                    self.notify_session_finished(session_id, suppress_errors=True)

        driver.quit = wrapped_quit

    def complete_run(self, run_id: str) -> dict[str, Any]:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/complete",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/cancel",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def start_heartbeat(self, run_id: str, interval: int = 30) -> HeartbeatThread:
        thread = HeartbeatThread(self.base_url, run_id, interval, auth=self._auth)
        thread.start()
        return thread


RunCleanupPolicy = Literal["complete", "cancel", "noop"]
RunCleanup = Callable[[], None]


def _apply_run_cleanup_policy(client: GridFleetClient, run_id: str, policy: RunCleanupPolicy) -> None:
    if policy == "complete":
        client.complete_run(run_id)
    elif policy == "cancel":
        client.cancel_run(run_id)


def register_run_cleanup(
    client: GridFleetClient,
    run_id: str,
    heartbeat_thread: HeartbeatThread | None = None,
    *,
    on_exit: RunCleanupPolicy = "noop",
    on_signal: RunCleanupPolicy = "cancel",
    install_signal_handlers: bool = False,
    chain_signals: bool = True,
    join_timeout_sec: float | None = 5.0,
) -> RunCleanup:
    """Register exit cleanup for a run and optionally install signal handlers.

    Normal process exit defaults to ``noop`` because atexit cannot know whether
    the run succeeded. Callers that know outcome should explicitly complete or
    cancel the run, or pass ``on_exit=`` when legacy auto-finalization is wanted.
    """

    called_lock = threading.Lock()
    called = False
    previous_handlers: dict[signal.Signals, Any] = {}

    def cleanup(policy: RunCleanupPolicy = on_exit) -> None:
        nonlocal called
        with called_lock:
            if called:
                return
            called = True
        if heartbeat_thread:
            heartbeat_thread.stop()
            heartbeat_thread.join(timeout=join_timeout_sec)
            if heartbeat_thread.is_alive():
                logger.warning("Heartbeat thread for run %s did not stop within %s seconds", run_id, join_timeout_sec)
        try:
            _apply_run_cleanup_policy(client, run_id, policy)
        except Exception:
            logger.warning("Failed to apply %s cleanup policy for run %s", policy, run_id, exc_info=True)

    def signal_cleanup(sig: int, frame: FrameType | None) -> None:
        cleanup(on_signal)
        if not chain_signals:
            return
        previous = previous_handlers.get(signal.Signals(sig))
        if callable(previous):
            previous(sig, frame)
        elif previous is signal.SIG_DFL:
            # Restore default and re-raise so the kernel applies it (e.g. SIGTERM terminates).
            signal.signal(sig, signal.SIG_DFL)
            signal.raise_signal(sig)
        # SIG_IGN: do nothing (intentional drop).

    atexit.register(cleanup)
    if install_signal_handlers:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, signal_cleanup)
    return cleanup
