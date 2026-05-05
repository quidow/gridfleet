"""Public GridFleet client helpers for external test suites."""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import signal
import threading
import time
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Sequence

import httpx

GRID_URL = os.getenv("GRID_URL", "http://localhost:4444")
GRIDFLEET_API_URL = os.getenv("GRIDFLEET_API_URL", "http://localhost:8000/api")
GRIDFLEET_TESTKIT_USERNAME = os.getenv("GRIDFLEET_TESTKIT_USERNAME")
GRIDFLEET_TESTKIT_PASSWORD = os.getenv("GRIDFLEET_TESTKIT_PASSWORD")

logger = logging.getLogger("gridfleet_testkit")


def _default_auth() -> httpx.BasicAuth | None:
    """Build httpx Basic auth from env vars, or return None when unset."""
    username = GRIDFLEET_TESTKIT_USERNAME
    password = GRIDFLEET_TESTKIT_PASSWORD
    if not username or not password:
        return None
    return httpx.BasicAuth(username, password)


class NoClaimableDevicesError(RuntimeError):
    """Raised when the manager reports that no run devices are claimable yet."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_sec: int,
        run_id: str = "",
        next_available_at: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.retry_after_sec = retry_after_sec
        self.next_available_at = next_available_at
        super().__init__(message)


class UnknownIncludeError(ValueError):
    """Backend rejected one or more `?include=` keys."""

    def __init__(self, values: list[str]) -> None:
        super().__init__(f"Backend rejected unknown include values: {values}")
        self.values = values


class ReserveCapabilitiesUnsupportedError(ValueError):
    """`?include=capabilities` is not supported on reserve."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or "include=capabilities is not supported on reserve; use include on claim_device")


def _raise_for_status(resp: Any, *, run_id: str) -> None:
    if resp.status_code == 409:
        try:
            payload = resp.json()
        except Exception:
            payload = None
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            details = error.get("details")
            if isinstance(details, dict) and details.get("error") == "no_claimable_devices":
                retry_after = details.get("retry_after_sec")
                if not isinstance(retry_after, int):
                    retry_after = 5
                next_available_at = details.get("next_available_at")
                raise NoClaimableDevicesError(
                    str(error.get("message") or "No unclaimed devices available in this run"),
                    run_id=run_id,
                    retry_after_sec=retry_after,
                    next_available_at=next_available_at if isinstance(next_available_at, str) else None,
                )
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


def _is_safe_release_conflict(resp: Any) -> bool:
    try:
        payload = resp.json()
    except Exception:
        return False
    detail = payload.get("detail") if isinstance(payload, dict) else None
    return isinstance(detail, str) and "is not claimed" in detail.lower()


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
        base_url: str = GRIDFLEET_API_URL,
        auth: httpx.BasicAuth | None = None,
    ):
        self.base_url = base_url.rstrip("/")
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

    def get_device_config(self, connection_target: str, reveal: bool = True) -> dict[str, Any]:
        """Fetch device config by looking up the current runtime connection target."""
        resp = httpx.get(
            f"{self.base_url}/devices",
            params={"connection_target": connection_target},
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        devices = cast("list[dict[str, Any]]", resp.json())
        if not devices:
            raise ValueError(f"No device found with connection target: {connection_target}")
        device_id = devices[0]["id"]
        config_resp = httpx.get(
            f"{self.base_url}/devices/{device_id}/config",
            params={"reveal": str(reveal).lower()},
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
    ) -> dict[str, Any]:
        """Reserve devices for a test run and return the manager response."""
        resp = httpx.post(
            f"{self.base_url}/runs",
            json={
                "name": name,
                "requirements": requirements,
                "ttl_minutes": ttl_minutes,
                "heartbeat_timeout_sec": heartbeat_timeout_sec,
                "created_by": created_by,
            },
            timeout=30,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def signal_ready(self, run_id: str) -> None:
        httpx.post(
            f"{self.base_url}/runs/{run_id}/ready",
            timeout=10,
            auth=self._auth,
        ).raise_for_status()

    def signal_active(self, run_id: str) -> None:
        httpx.post(
            f"{self.base_url}/runs/{run_id}/active",
            timeout=10,
            auth=self._auth,
        ).raise_for_status()

    def heartbeat(self, run_id: str) -> dict[str, Any]:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/heartbeat",
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def claim_device(
        self,
        run_id: str,
        *,
        worker_id: str,
        include: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        include_tuple = _normalize_include(include)
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/claim",
            json={"worker_id": worker_id},
            params=_include_param(include_tuple),
            timeout=10,
            auth=self._auth,
        )
        _raise_for_status(resp, run_id=run_id)
        return cast("dict[str, Any]", resp.json())

    def release_device(self, run_id: str, *, device_id: str, worker_id: str) -> None:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/release",
            json={"device_id": device_id, "worker_id": worker_id},
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()

    def release_device_safe(self, run_id: str, *, device_id: str, worker_id: str) -> bool:
        """Release a claim while tolerating already-terminal run/device states.

        Returns True when the manager accepts the release, False when the run is
        gone or the claim is explicitly already unclaimed. Other HTTP errors,
        including wrong-worker conflicts, still raise.
        """
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/release",
            json={"device_id": device_id, "worker_id": worker_id},
            timeout=10,
            auth=self._auth,
        )
        if resp.status_code == 404 or (resp.status_code == 409 and _is_safe_release_conflict(resp)):
            return False
        resp.raise_for_status()
        return True

    def release_device_with_cooldown(
        self,
        run_id: str,
        *,
        device_id: str,
        worker_id: str,
        reason: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/devices/{device_id}/release-with-cooldown",
            json={"worker_id": worker_id, "reason": reason, "ttl_seconds": ttl_seconds},
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
        return cast("dict[str, Any]", resp.json())

    def claim_device_with_retry(
        self,
        run_id: str,
        *,
        worker_id: str,
        max_wait_sec: int = 300,
        include: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max_wait_sec
        include_tuple = _normalize_include(include)
        while True:
            try:
                return self.claim_device(run_id, worker_id=worker_id, include=include_tuple)
            except NoClaimableDevicesError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(exc.retry_after_sec, max(1, int(remaining))))

    def report_preparation_failure(
        self,
        run_id: str,
        device_id: str,
        message: str,
        source: str = "ci_preparation",
    ) -> dict[str, Any]:
        resp = httpx.post(
            f"{self.base_url}/runs/{run_id}/devices/{device_id}/preparation-failed",
            json={"message": message, "source": source},
            timeout=10,
            auth=self._auth,
        )
        resp.raise_for_status()
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
        """Extract session metadata from an Appium driver and register it."""
        capabilities = getattr(driver, "capabilities", {})
        if not isinstance(capabilities, dict):
            capabilities = {}
        session_id = getattr(driver, "session_id", None)
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("Created Appium driver did not expose a session ID")
        device_id = capabilities.get("appium:gridfleet:deviceId") or capabilities.get("gridfleet:deviceId")
        connection_target = capabilities.get("appium:udid") or capabilities.get("appium:deviceName")
        return self.register_session(
            session_id=session_id,
            test_name=test_name,
            device_id=device_id if isinstance(device_id, str) and device_id else None,
            connection_target=connection_target if isinstance(connection_target, str) and connection_target else None,
            requested_capabilities=capabilities,
            run_id=run_id,
            suppress_errors=suppress_errors,
        )

    def complete_run(self, run_id: str) -> None:
        httpx.post(
            f"{self.base_url}/runs/{run_id}/complete",
            timeout=10,
            auth=self._auth,
        ).raise_for_status()

    def cancel_run(self, run_id: str) -> None:
        httpx.post(
            f"{self.base_url}/runs/{run_id}/cancel",
            timeout=10,
            auth=self._auth,
        ).raise_for_status()

    def start_heartbeat(self, run_id: str, interval: int = 30) -> HeartbeatThread:
        thread = HeartbeatThread(self.base_url, run_id, interval, auth=self._auth)
        thread.start()
        return thread


def register_run_cleanup(
    client: GridFleetClient,
    run_id: str,
    heartbeat_thread: HeartbeatThread | None = None,
) -> None:
    """Register exit and signal handlers that release reserved devices."""

    def cleanup(*_args: object) -> None:
        if heartbeat_thread:
            heartbeat_thread.stop()
        try:
            client.complete_run(run_id)
        except Exception:
            with contextlib.suppress(Exception):
                client.cancel_run(run_id)

    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
